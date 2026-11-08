"""Stream-contract adapter for an allocator loaded from a separate checkout."""

import copy
import importlib.util
import json
import math
from collections import defaultdict
from pathlib import Path

from smartgate_stream.gates import GateState, LEG_FIELDS
from smartgate_stream.inventory import catalog, gate_fits
from smartgate_stream.validation import flight_key, require, validate_gate_state


def load_algorithm(path):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location("smartgate_allocator", path)
    require(spec is not None and spec.loader is not None, "algorithm must be a Python module")
    algorithm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(algorithm)
    for name in ("run_julia", "repair_assignments_python"):
        require(callable(getattr(algorithm, name, None)), f"algorithm must provide {name}")
    require(hasattr(algorithm, "SOLVER_BACKEND"), "algorithm must provide SOLVER_BACKEND")
    return algorithm


def parking_sessions(state):
    """Project current ground visits and future arrivals from the stream model."""
    sessions, references = {}, {}

    def add(sid, aircraft, start, end, keys, policy=None):
        if end <= start or start >= state.day_end:
            return
        policy = policy or {}
        diversion = policy.get("action") == "DivertFlight"
        emergency = policy.get("emergencyState")
        reason = policy.get("reason")
        remote = emergency == "Emergency" or (emergency is None and diversion and reason in ("medical", "diversion"))
        turnback = emergency == "Turnback" or (emergency is None and diversion and reason == "turnback")
        sessions[sid] = {
            "id": sid, "registration": aircraft["registration"],
            "aircraftType": aircraft["aircraftType"],
            "widthClass": catalog()["aircraft_widths"][aircraft["aircraftType"]],
            "start": start.timestamp() / 60, "end": min(end, state.day_end).timestamp() / 60,
            "priority": (diversion and not remote) or turnback,
            "remoteAllowed": remote, "turnback": turnback,
            "initiallyParked": aircraft["registration"] in state.grounded and start == state.now,
        }
        references[sid] = keys

    def session_id(key):
        return json.dumps(key, separators=(",", ":"))

    movements = defaultdict(list)
    for key, record in sorted(state.flights.items(), key=lambda item: repr(item[0])):
        if record["cancelled"]:
            continue
        tail = record["aircraft"]["registration"]
        if record["leg"]["arrivalStation"] == "YYZ" and not record["arrived"]:
            movements[tail].append((record["arrival"], 1, key))
        if record["leg"]["departureStation"] == "YYZ" and not record["departed"]:
            movements[tail].append((record["departure"], 0, key))
    for tail in sorted(set(state.grounded) | set(movements)):
        opened = None
        if tail in state.grounded:
            keys = [key for key, record in state.flights.items() if not record["cancelled"]
                    and record["aircraft"]["registration"] == tail and record["arrived"]
                    and record["leg"]["arrivalStation"] == "YYZ" and state._current_visit(record)]
            policy = next((state.flights[key].get("policy") for key in keys), None)
            opened = ("ground:" + tail, state.grounded[tail], state.now, keys, policy)
        for when, arriving, key in sorted(movements[tail], key=lambda item: (item[0], item[1], repr(item[2]))):
            record = state.flights[key]
            if arriving:
                if opened is None:
                    opened = (session_id(key), record["aircraft"], when, [], record.get("policy"))
                # A cancelled departure can temporarily join two known visits.
                opened[3].append(key)
            elif opened is not None:
                if record["leg"]["arrivalStation"] != "YYZ":
                    opened[3].append(key)
                add(opened[0], opened[1], opened[2], when, opened[3], opened[4])
                opened = None
        if opened is not None:
            add(opened[0], opened[1], opened[2], state.day_end, opened[3], opened[4])
    return sessions, references


def resources():
    """Use one allocator resource per shared physical footprint."""
    groups = defaultdict(list)
    for gate, footprint in catalog()["footprints"].items():
        groups[footprint].append(gate)
    return [{"id": footprint, "widthClass": max(catalog()["gate_widths"][gate] for gate in gates),
             "kind": "remote" if all(catalog()["gates"][gate] is None for gate in gates) else "gate"}
            for footprint, gates in sorted(groups.items())]


class Matcher:
    def __init__(self, algorithm):
        self.algorithm = algorithm
        self.state = None
        self.sessions = {}
        self.bookings = {}
        self.gate_starts = {}
        self.conflicts = []

    def update(self, message, timestamp):
        initializing = message.get("action") == "InitializeAirport"
        require(initializing or self.state is not None, "first message must be InitializeAirport")
        state = GateState() if initializing else copy.deepcopy(self.state)
        state.apply_message(message, timestamp)
        require(state.initialized, "matcher requires InitializeAirport")
        validate_gate_state(message.get("gateState"))
        require(message["gateState"] == state.gates, "gateState differs from the last applied decisions")
        require(message.get("unassignedAircraft") == state.snapshot()["unassigned_aircraft"],
                "unassignedAircraft differs from the known ground state")
        if message["action"] in ("CreateFlight", "DivertFlight", "ReplaceFlight", "InitializeAirport"):
            for flight in message.get("flights", [message]):
                for leg in flight.get("legs", []):
                    key = flight_key(flight["flight"]["carrier"], leg)
                    state.flights[key]["policy"] = {field: flight.get(field) for field in ("action", "emergencyState", "reason")}

        sessions, references = parking_sessions(state)
        gates = resources()
        outages = defaultdict(list)
        for gate, start, end in state.outages:
            outages[catalog()["footprints"][gate]].append([
                start.timestamp() / 60, (end or state.day_end).timestamp() / 60])
        positions = {plane["registration"]: gate for gate, plane in state.gates.items() if plane is not None}
        fixed = {}
        for sid, session in sessions.items():
            gate = positions.get(session["registration"]) if sid.startswith("ground:") else None
            if gate is not None:
                footprint = catalog()["footprints"][gate]
                if not any(session["start"] < end and start < session["end"] for start, end in outages[footprint]):
                    fixed[sid] = footprint

        bookings = {} if initializing else self.bookings
        starts = {} if initializing else self.gate_starts
        unresolved = set()
        initial_solve = initializing
        while True:
            blocked = copy.deepcopy(outages)
            for sid, footprint in fixed.items():
                blocked[footprint].append([sessions[sid]["start"], sessions[sid]["end"]])
            movable = {sid: session for sid, session in sessions.items() if sid not in fixed}
            previous = {sid: gate for sid, gate in bookings.items() if sid in movable}
            starts = {sid: starts[sid] for sid in previous}
            changed = {sid for sid, session in movable.items()
                       if sid not in previous or self.sessions.get(sid) != session
                       or starts[sid] > session["start"]
                       or any(starts[sid] < end and start < session["end"] for start, end in blocked[previous[sid]])}
            if initial_solve:
                result = self.algorithm.run_julia(self.algorithm.SOLVER_BACKEND, {
                    "mode": "initial", "sessions": list(movable.values()), "gates": gates, "outages": dict(blocked)})
                previous, starts = self._result(result, movable)
                changed = set(movable) - set(previous)
                initial_solve = False
            if changed:
                result = self.algorithm.repair_assignments_python(movable, gates, dict(blocked), previous, changed, starts)
                retained = {sid: gate for sid, gate in previous.items() if sid not in changed}
                new, new_starts = self._result(result, {sid: movable[sid] for sid in changed})
                previous = {**retained, **new}
                starts = {**{sid: starts[sid] for sid in retained}, **new_starts}
            bookings = {**fixed, **previous}
            starts.update({sid: sessions[sid]["start"] for sid in fixed})
            stranded = {sid for sid, session in movable.items() if sid.startswith("ground:")
                        and session["registration"] in positions
                        and (sid not in bookings or starts[sid] > session["start"])}
            if not stranded:
                break
            # A failed relocation never frees a physical occupant's position.
            for sid in stranded:
                fixed[sid] = catalog()["footprints"][positions[sessions[sid]["registration"]]]
            unresolved.update(stranded)
        self._validate_bookings(sessions, bookings, starts, gates, outages, unresolved)

        desired, moves = {}, []
        for sid, session in sessions.items():
            footprint = bookings.get(sid)
            current = positions.get(session["registration"]) if sid.startswith("ground:") else None
            ready = footprint is not None and starts[sid] <= session["start"]
            gate = None
            if ready:
                aircraft = {"aircraftType": session["aircraftType"]}
                gate = current if current is not None and catalog()["footprints"][current] == footprint else next(
                    (gate for gate in state.gates if catalog()["footprints"][gate] == footprint and gate_fits(gate, aircraft)), None)
                require(gate is not None, "allocator chose a resource that cannot fit the aircraft")
            # shortcut: queued gate entry needs a later webhook; add movement callbacks for entry between announcements.
            for key in references[sid]:
                require(key not in desired or desired[key] == gate, "overlapping ground visits share a flight leg")
                desired[key] = gate
            if current != gate and sid.startswith("ground:") and gate is not None:
                moves.append({"registration": session["registration"], "gate": gate})

        decisions = {"assignments": [], "moves": moves}
        for key in sorted(set(state.assignments) | set(desired), key=repr):
            if key not in state.flights or state.flights[key]["cancelled"]:
                continue
            gate = desired.get(key)
            if state.assignments.get(key) != gate:
                decisions["assignments"].append({"carrier": key[0],
                    "leg": {field: state.flights[key]["leg"].get(field) for field in LEG_FIELDS}, "gate": gate})
        state.apply_decisions(decisions, initializing=initializing)
        require(len(json.dumps(decisions).encode()) <= 1024 * 1024, "decisions exceed the stream's 1 MiB limit")
        self.state, self.sessions, self.bookings, self.gate_starts = state, sessions, bookings, starts
        self.conflicts = [{"registration": sessions[sid]["registration"],
                           "gate": positions[sessions[sid]["registration"]],
                           "reason": "no_immediate_relocation_for_gate_outage"} for sid in sorted(unresolved)]
        return decisions

    @staticmethod
    def _result(result, sessions):
        require(isinstance(result, dict), "allocator result must be an object")
        assignments, starts = {}, {}
        gate_starts = result.get("gate_starts", {})
        require(isinstance(gate_starts, dict), "allocator gate_starts must be an object")
        for field in ("assignments", "reassignments", "retained_assignments"):
            pairs = result.get(field, [])
            require(isinstance(pairs, list), f"allocator {field} must be an array")
            for pair in pairs:
                require(isinstance(pair, (list, tuple)) and len(pair) == 2, "invalid allocator assignment")
                sid, gate = pair
                require(isinstance(sid, str) and sid in sessions and sid not in assignments,
                        "unknown or duplicate allocator session")
                require(isinstance(gate, str), "allocator gate must be a string")
                assignments[sid] = gate
                start = gate_starts.get(sid, sessions[sid]["start"])
                require(type(start) in (int, float) and math.isfinite(start), "allocator gate entry must be finite")
                starts[sid] = start
        return assignments, starts

    @staticmethod
    def _validate_bookings(sessions, assignments, starts, gates, outages, unresolved):
        capacities = {gate["id"]: gate["widthClass"] for gate in gates}
        occupied = defaultdict(list)
        for sid, gate in sorted(assignments.items()):
            session = sessions[sid]
            start, end = starts[sid], session["end"]
            require(gate in capacities and session["widthClass"] <= capacities[gate], "invalid allocator resource capacity")
            require(session["start"] <= start < end, "allocator gate entry is outside the ground visit")
            require(sid in unresolved or not any(start < b and a < end for a, b in outages[gate]),
                    "allocator bookings overlap a gate outage")
            require(not any(start < other_end and other_start < end for other_start, other_end in occupied[gate]),
                    "allocator bookings overlap another aircraft or a gate outage")
            occupied[gate].append([start, end])
