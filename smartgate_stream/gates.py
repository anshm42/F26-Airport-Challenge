"""Current passenger occupancy, kept separate from future matcher choices."""

import copy
from datetime import datetime, time, timedelta, timezone

from .inventory import (AIRCRAFT_FIELDS, canonical_gate, catalog, gate_blocker, gate_fits,
                        initial_gate_state, outage_gate)
from .validation import (flight_key, parse_utc, require, string_field,
                         validate_leg_reference, validate_message)


LEG_FIELDS = ("flightNumber", "flightSuffix", "originatingDate", "departureStation", "arrivalStation")


def utc_string(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def leg_times(leg):
    zones = catalog()["zones"]
    require(leg["departureStation"] in zones and leg["arrivalStation"] in zones,
            "station time zone is not configured")
    departure = datetime.fromisoformat(leg["originatingDate"] + "T" + leg["scheduledDepartureTime"]).replace(
        tzinfo=zones[leg["departureStation"]]).astimezone(timezone.utc)
    arrival_zone = zones[leg["arrivalStation"]]
    arrival_day = departure.astimezone(arrival_zone).date()
    arrival = datetime.combine(arrival_day, time.fromisoformat(leg["scheduledArrivalTime"]), arrival_zone)
    if arrival.astimezone(timezone.utc) < departure:
        arrival = datetime.combine(arrival_day + timedelta(days=1), arrival.timetz(), arrival_zone)
    return departure, arrival.astimezone(timezone.utc)


class GateState:
    def __init__(self):
        self.gates = dict.fromkeys(catalog()["gates"])
        self.grounded = {}
        self.aircraft = {}
        self.flights = {}
        self.assignments = {}
        self.outages = []
        self.now = None
        self.day_end = None
        self.initialized = False

    def _aircraft(self, aircraft):
        plane = {field: aircraft[field] for field in AIRCRAFT_FIELDS}
        tail = plane["registration"]
        require(self.aircraft.get(tail, plane) == plane, "conflicting aircraft identity")
        self.aircraft[tail] = plane
        return plane

    def _route(self, carrier, leg):
        return carrier, leg["flightNumber"], leg.get("flightSuffix"), leg["originatingDate"]

    def _add_route(self, message, *, replace=False):
        aircraft = self._aircraft(message["flight"])
        route = self._route(aircraft["carrier"], message["legs"][0])
        previous = {key: record for key, record in self.flights.items() if record["route"] == route}
        previous_plans = {key: self.assignments[key] for key, record in self.flights.items()
                          if record["route"] == route and key in self.assignments}
        require(replace or not previous, "flight route already exists")
        if replace:
            require(previous, "cannot replace an unknown route")
            for key in [key for key, value in self.flights.items() if value["route"] == route]:
                del self.flights[key]
                self.assignments.pop(key, None)
        for leg in message["legs"]:
            departure, arrival = leg_times(leg)
            key = flight_key(aircraft["carrier"], leg)
            require(key not in self.flights, "duplicate flight leg")
            old = previous.get(key, {})
            self.flights[key] = {
                "leg": copy.deepcopy(leg), "aircraft": aircraft.copy(), "route": route,
                "departure": departure, "arrival": arrival, "cancelled": False,
                "departed": old.get("departed", departure < self.now),
                "arrived": old.get("arrived", False),
            }
            if key in previous_plans:
                self.assignments[key] = previous_plans[key]

    def _outage(self, message, timestamp):
        gate = outage_gate(message["gate"])
        zone = catalog()["zones"]["YYZ"]
        day = timestamp.astimezone(zone).date()
        start = datetime.combine(day, time.fromisoformat(message["scheduledStartTime"]), zone)
        end = None
        if "scheduledEndTime" in message:
            end = datetime.combine(day, time.fromisoformat(message["scheduledEndTime"]), zone)
            if end < start:
                end = datetime.combine(day + timedelta(days=1), end.timetz(), zone)
            end = end.astimezone(timezone.utc)
        self.outages.append((gate, start.astimezone(timezone.utc), end))

    def apply_message(self, message, timestamp):
        validate_message(message)
        timestamp = parse_utc(timestamp) if isinstance(timestamp, str) else timestamp
        action = message["action"]
        if action == "InitializeAirport":
            require(not self.initialized and not self.flights, "initialization must be first")
            self.initialized = True
            self.now = timestamp
            zone = catalog()["zones"]["YYZ"]
            self.day_end = datetime.combine(timestamp.astimezone(zone).date() + timedelta(days=1),
                                            time(), zone).astimezone(timezone.utc)
            parked, self.gates = initial_gate_state(message["parkedAircraft"])
            self.grounded = {plane["registration"]: self._aircraft(plane) for plane in parked}
            for flight in message["flights"]:
                self._add_route(flight)
            for record in self.flights.values():
                record["departed"] = record["departure"] <= timestamp
                record["arrived"] = record["arrival"] <= timestamp
            for outage in message["gateOutages"]:
                self._outage(outage, timestamp)
            require(all(not self._out_of_service(gate, timestamp) for gate, plane in self.gates.items()
                        if plane is not None), "initial aircraft occupies an out-of-service position")
            return
        if self.now is None:
            self.now = timestamp
        self.advance(timestamp)
        if action in ("CreateFlight", "DivertFlight", "ReplaceFlight"):
            self._add_route(message, replace=action == "ReplaceFlight")
        elif action == "GateOutage":
            self._outage(message, timestamp)
        else:
            keys = [flight_key(message["flight"]["carrier"], leg) for leg in message["legs"]]
            require(all(key in self.flights for key in keys), "update references an unknown flight")
            if action == "UpdateTiming":
                for key, leg in zip(keys, message["legs"]):
                    record = self.flights[key]
                    record["leg"] = copy.deepcopy(leg)
                    record["departure"], record["arrival"] = leg_times(leg)
            else:
                target = self.flights[keys[0]]
                all_legs = (action == "ReinstateFlight"
                            or message.get("cancelScope", {}).get("type") == "AllLegs"
                            or message.get("equipmentUpdate", {}).get("scope") == "AllLegs")
                affected = [value for key, value in self.flights.items()
                            if value["route"] == target["route"]] if all_legs else [target]
                if action == "UpdateEquipment":
                    old_tails = {record["aircraft"]["registration"] for record in affected}
                    new_plane = self._aircraft(message["flight"]).copy()
                    new_tail = new_plane["registration"]
                    for record in affected:
                        record["aircraft"] = new_plane.copy()
                    # Equipment changes can change registration. Keep the
                    # stream's grounded index and gateState occupant identity
                    # synchronized so matcher moves reference the new tail.
                    for old_tail in old_tails:
                        if old_tail == new_tail or old_tail not in self.grounded:
                            continue
                        require(new_tail not in self.grounded,
                                "equipment update would duplicate a grounded registration")
                        self.grounded.pop(old_tail)
                        self.grounded[new_tail] = new_plane.copy()
                        self.gates = {
                            gate: (new_plane.copy()
                                   if occupant is not None and occupant["registration"] == old_tail
                                   else occupant)
                            for gate, occupant in self.gates.items()
                        }
                else:
                    for record in affected:
                        record["cancelled"] = action == "CancelFlight"
        self.advance(timestamp)

    def _pending(self):
        pending = []
        for key, record in self.flights.items():
            if record["cancelled"]:
                continue
            if record["leg"]["departureStation"] == "YYZ" and not record["departed"]:
                pending.append((record["departure"], 0, key, "departed"))
            if record["leg"]["arrivalStation"] == "YYZ" and not record["arrived"]:
                pending.append((record["arrival"], 1, key, "arrived"))
        return sorted(pending, key=lambda value: (value[0], value[1], repr(value[2])))

    def next_movement(self):
        return next((when for when, _, _, _ in self._pending()
                     if self.day_end is None or when < self.day_end), None)

    def _out_of_service(self, gate, when):
        footprint = catalog()["footprints"][gate]
        return any(catalog()["footprints"][other] == footprint and start <= when
                   and (end is None or when < end) for other, start, end in self.outages)

    def _next_outbound(self, record):
        tail = record["aircraft"]["registration"]
        options = [(value["departure"], key) for key, value in self.flights.items()
                   if value["aircraft"]["registration"] == tail and not value["cancelled"]
                   and value["leg"]["departureStation"] == "YYZ"
                   and value["departure"] >= record["arrival"]]
        return min(options, key=lambda item: (item[0], repr(item[1])))[1] if options else None

    def advance(self, timestamp):
        require(self.now is None or timestamp >= self.now, "gate-state time cannot go backwards")
        for when, _, key, movement in self._pending():
            if when > timestamp:
                break
            record = self.flights[key]
            plane, tail = record["aircraft"], record["aircraft"]["registration"]
            self.now = max(self.now, when) if self.now is not None else when
            if movement == "departed":
                require(not self.initialized or tail in self.grounded,
                        "scheduled departure has no aircraft on the ground at YYZ")
                self.gates = {gate: None if occupant is not None and occupant["registration"] == tail else occupant
                              for gate, occupant in self.gates.items()}
                self.grounded.pop(tail, None)
            else:
                require(tail not in self.grounded, "aircraft cannot arrive while already on the ground at YYZ")
                self.grounded[tail] = plane.copy()
                gate = self.assignments.get(key) or self.assignments.get(self._next_outbound(record))
                if (gate is not None and gate_fits(gate, plane) and gate_blocker(self.gates, gate) is None
                        and not self._out_of_service(gate, self.now)):
                    self.gates[gate] = plane.copy()
            record[movement] = True
        self.now = timestamp

    def _current_visit(self, record):
        tail = record["aircraft"]["registration"]
        if tail not in self.grounded:
            return False
        leg = record["leg"]
        if leg["arrivalStation"] == "YYZ" and record["arrived"]:
            return not any(value["aircraft"]["registration"] == tail and value["departed"]
                           and value["leg"]["departureStation"] == "YYZ"
                           and record["arrival"] <= value["departure"] <= self.now
                           for value in self.flights.values())
        if leg["departureStation"] == "YYZ" and not record["departed"]:
            return not any(value["aircraft"]["registration"] == tail and not value["cancelled"]
                           and value["leg"]["arrivalStation"] == "YYZ"
                           and self.now < value["arrival"] <= record["departure"]
                           for value in self.flights.values())
        return False

    def apply_decisions(self, response, *, initializing=False):
        if response is None:
            return
        candidate = copy.deepcopy(self)
        candidate._decisions(response, initializing=initializing)
        self.__dict__.update(candidate.__dict__)

    def _decisions(self, response, *, initializing):
        require(isinstance(response, dict) and set(response) <= {"assignments", "moves"},
                "matcher response must contain only assignments and moves")
        assignments, moves = response.get("assignments", []), response.get("moves", [])
        require(isinstance(assignments, list) and isinstance(moves, list), "matcher decisions must be arrays")
        targets, references = {}, set()
        for decision in assignments:
            require(isinstance(decision, dict) and set(decision) == {"carrier", "leg", "gate"}, "invalid assignment fields")
            string_field(decision, "carrier", r"[A-Z0-9]{2}")
            leg = decision["leg"]
            validate_leg_reference(leg)
            require(set(leg) <= set(LEG_FIELDS) | {"trafficType"}, "assignment requires a leg reference, not a complete leg")
            key = flight_key(decision["carrier"], leg)
            require(key in self.flights and key not in references, "unknown or duplicate assignment reference")
            references.add(key)
            record = self.flights[key]
            require(not record["cancelled"], "cannot assign a cancelled flight")
            gate = canonical_gate(decision["gate"]) if decision["gate"] is not None else None
            if gate is None:
                self.assignments.pop(key, None)
            else:
                require(gate_fits(gate, record["aircraft"]),
                        "matcher chose a gate without matched IFATC capacity for this aircraft")
                self.assignments[key] = gate
                if not initializing and self._current_visit(record):
                    tail = record["aircraft"]["registration"]
                    require(targets.get(tail, gate) == gate, "conflicting current-visit assignments")
                    targets[tail] = gate
        moved = set()
        for move in moves:
            require(isinstance(move, dict) and set(move) == {"registration", "gate"}, "invalid move fields")
            tail = move["registration"]
            require(isinstance(tail, str) and tail in self.grounded and tail not in moved,
                    "move references an unknown, airborne, or duplicate aircraft")
            moved.add(tail)
            gate = canonical_gate(move["gate"])
            require(targets.get(tail, gate) == gate, "move conflicts with a current-visit assignment")
            targets[tail] = gate
        for key, record in self.flights.items():
            if record["arrived"] or record["cancelled"] or record["leg"]["arrivalStation"] != "YYZ":
                continue
            outbound = self._next_outbound(record)
            arrival_gate, departure_gate = self.assignments.get(key), self.assignments.get(outbound)
            require(arrival_gate is None or departure_gate is None or arrival_gate == departure_gate,
                    "arrival and departure assignments conflict for one ground visit")
        if initializing:
            for key in references:
                record = self.flights[key]
                gate = self.assignments.get(key)
                if gate is not None and self._current_visit(record):
                    tail = record["aircraft"]["registration"]
                    current = next((key for key, plane in self.gates.items()
                                    if plane is not None and plane["registration"] == tail), None)
                    require(targets.get(tail, current) == gate,
                            "changing an initial occupied gate requires an explicit move")
        remaining = {gate: None if plane is not None and plane["registration"] in targets else plane
                     for gate, plane in self.gates.items()}
        for tail, gate in targets.items():
            require(gate_fits(gate, self.grounded[tail]),
                    "matcher chose a gate without matched IFATC capacity for this aircraft")
            require(gate_blocker(remaining, gate) is None, "matcher chose an occupied or overlapping position")
            require(not self._out_of_service(gate, self.now), "matcher chose an out-of-service position")
            # shortcut: moves are instantaneous; add tow durations when movement scheduling is required.
            remaining[gate] = self.grounded[tail].copy()
        self.gates = remaining

    def snapshot(self):
        occupied = {plane["registration"] for plane in self.gates.values() if plane is not None}
        planned = []
        for key in sorted(self.assignments, key=lambda value: tuple(part or "" for part in value)):
            record = self.flights[key]
            planned.append({"carrier": key[0], "leg": {field: record["leg"].get(field) for field in LEG_FIELDS},
                            "gate": self.assignments[key]})
        return {"gate_state": copy.deepcopy(self.gates),
                "gate_state_time": utc_string(self.now) if self.now is not None else None,
                "unassigned_aircraft": [plane.copy() for tail, plane in sorted(self.grounded.items()) if tail not in occupied],
                "planned_assignments": planned}

    def check_baseline_capacity(self):
        """Prove the known baseline fits at every movement/outage boundary."""
        candidate = copy.deepcopy(self)
        candidate.assignments.clear()
        times = {self.now} | {when for when, _, _, _ in self._pending()}
        times.update(when for _, start, end in self.outages for when in (start, end) if when is not None)
        for when in sorted(when for when in times if when >= self.now and (self.day_end is None or when < self.day_end)):
            candidate.advance(when)
            capacities = {}
            for gate in candidate.gates:
                footprint = catalog()["footprints"][gate]
                if not candidate._out_of_service(gate, when):
                    capacities[footprint] = max(capacities.get(footprint, "A"), catalog()["gate_widths"][gate])
            sizes = [catalog()["aircraft_widths"][plane["aircraftType"]]
                     for plane in candidate.grounded.values()]
            # Size classes form nested pools: every larger aircraft also consumes the smaller pool.
            for size in "ABCDEF":
                needed = sum(width >= size for width in sizes)
                available = sum(width >= size for width in capacities.values())
                require(needed <= available,
                        f"baseline schedule exceeds passenger gate capacity at {utc_string(when)} "
                        f"(class {size} or larger: {needed} aircraft, {available} usable positions)")

    def request(self, message):
        request = copy.deepcopy(message)
        for leg in request.get("legs", []):
            key = flight_key(request["flight"]["carrier"], leg)
            leg["trafficType"] = self.flights[key]["leg"]["trafficType"]
        state = self.snapshot()
        request["gateState"] = state["gate_state"]
        request["unassignedAircraft"] = state["unassigned_aircraft"]
        return request
