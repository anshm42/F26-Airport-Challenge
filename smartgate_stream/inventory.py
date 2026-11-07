"""Passenger resources shared by generation, validation, and live gate state."""

import json
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo


DATA = Path(__file__).resolve().parents[1] / "data"
AIRCRAFT_FIELDS = ("carrier", "registration", "aircraftType")


@cache
def catalog():
    airport = json.loads((DATA / "pearson.json").read_text())
    aircraft = json.loads((DATA / "aircraft.json").read_text())
    profiles = json.loads((DATA / "stream_profiles.json").read_text())
    gates, aliases, footprints = {}, {}, {}
    for terminal, inventory in airport["passengerGateGroups"]["terminals"].items():
        for group in inventory["groups"]:
            gates[group[0]] = terminal
            footprints[group[0]] = group[0]
            for alias in group:
                aliases[alias] = group[0]
    for stand in airport["ifatcStands"]["records"]:
        name = stand["name"]
        if name.startswith("Remote Stand H"):
            gates[name] = None
            aliases[name] = name
            # shortcut: H variants are exclusive until a verified overlap matrix is available.
            footprints[name] = name.rstrip("AB")
    configured = {code for carrier in profiles["carriers"]
                  for service in carrier["services"] for code in service["aircraft"]}
    passenger = {record["aircraftType"] for record in aircraft["aircraftTypes"]["records"]
                 if record["serviceTypes"] == ["Passenger"]}
    return {
        "gates": gates, "aliases": aliases, "footprints": footprints,
        "passenger_types": configured & passenger,
        "airlines": {record["carrier"]: record["terminal"]
                     for record in airport["airlineTerminals"]["records"]},
        "zones": {station: ZoneInfo(record["timeZone"])
                  for station, record in profiles["airports"].items()},
    }


def canonical_gate(value):
    if not isinstance(value, str) or value not in catalog()["aliases"]:
        raise ValueError(f"unknown passenger gate: {value!r}")
    return catalog()["aliases"][value]


def passenger_aircraft(aircraft):
    return aircraft.get("aircraftType") in catalog()["passenger_types"]


def gate_blocker(gates, destination, registration=None):
    footprint = catalog()["footprints"][destination]
    return next((gate for gate, plane in gates.items()
                 if plane is not None and plane["registration"] != registration
                 and catalog()["footprints"][gate] == footprint), None)


def initial_gate_state(parked_aircraft, *, assign=False):
    gates = dict.fromkeys(catalog()["gates"])
    parked, registrations = [], set()
    for aircraft in parked_aircraft:
        if not passenger_aircraft(aircraft):
            raise ValueError("gate occupants must use a configured commercial passenger aircraft type")
        tail = aircraft["registration"]
        if tail in registrations:
            raise ValueError("duplicate parked aircraft")
        registrations.add(tail)
        if "gate" in aircraft:
            gate = canonical_gate(aircraft["gate"])
        elif assign:
            terminal = catalog()["airlines"].get(aircraft["carrier"])
            gate = next((gate for gate, owner in catalog()["gates"].items()
                         if gate_blocker(gates, gate) is None
                         and (terminal is None or owner is None or owner == terminal)), None)
            if gate is None:
                raise ValueError("not enough passenger gates for initially parked aircraft")
        else:
            raise ValueError("each initially parked aircraft requires a gate; regenerate the legacy scenario")
        if gate_blocker(gates, gate) is not None:
            raise ValueError("initial passenger gate is occupied more than once")
        plane = {field: aircraft[field] for field in AIRCRAFT_FIELDS}
        gates[gate] = plane
        parked.append({**plane, "gate": gate})
    return parked, gates
