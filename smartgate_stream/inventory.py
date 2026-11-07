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
    mapping = {row["gate"]: row for row in airport["passengerStandMapping"]["records"]}
    gates, aliases = {}, {}
    for terminal, inventory in airport["passengerGateGroups"]["terminals"].items():
        for group in inventory["groups"]:
            if group[0] not in mapping:
                continue
            gates[group[0]] = terminal
            for alias in group:
                aliases[alias] = group[0]
    for stand in airport["ifatcStands"]["records"]:
        name = stand["name"]
        if name.startswith("Remote Stand H"):
            gates[name] = None
            aliases[name] = name
    configured = {code for carrier in profiles["carriers"]
                  for service in carrier["services"] for code in service["aircraft"]}
    passenger = {record["aircraftType"] for record in aircraft["aircraftTypes"]["records"]
                 if record["serviceTypes"] == ["Passenger"]}
    return {
        "gates": gates, "aliases": aliases,
        "gate_stands": {gate: mapping[gate]["ifatcStandNames"] for gate in gates},
        "stand_gates": {name: gate for gate in gates for name in mapping[gate]["ifatcStandNames"]},
        "footprints": {gate: mapping[gate]["conflictGroup"] for gate in gates},
        "gate_widths": {gate: mapping[gate]["widthClass"] for gate in gates},
        "aircraft_widths": {row["aircraftType"]: row["widthClass"]
                            for row in aircraft["aircraftTypes"]["records"]},
        "passenger_types": configured & passenger,
        "airlines": {record["carrier"]: record["terminal"]
                     for record in airport["airlineTerminals"]["records"]},
        "zones": {station: ZoneInfo(record["timeZone"])
                  for station, record in profiles["airports"].items()},
        "countries": {station: record["countryCode"] for station, record in profiles["airports"].items()},
    }


def canonical_gate(value):
    if not isinstance(value, str) or value not in catalog()["aliases"]:
        raise ValueError(f"unknown passenger gate: {value!r}")
    return catalog()["aliases"][value]


def outage_gate(value):
    if not isinstance(value, str) or value not in catalog()["stand_gates"]:
        raise ValueError(f"unknown IFATC passenger stand: {value!r}")
    return catalog()["stand_gates"][value]


def passenger_aircraft(aircraft):
    return aircraft.get("aircraftType") in catalog()["passenger_types"]


def traffic_type(origin, destination):
    countries = catalog()["countries"]
    start, end = countries[origin], countries[destination]
    if start == end:
        return "Domestic"
    return "Transborder" if {start, end} == {"CA", "US"} else "International"


def gate_fits(gate, aircraft):
    capacity = catalog()["gate_widths"][gate]
    size = catalog()["aircraft_widths"].get(aircraft.get("aircraftType"))
    return passenger_aircraft(aircraft) and size is not None and capacity is not None and size <= capacity


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
                         if gate_fits(gate, aircraft) and gate_blocker(gates, gate) is None
                         and (terminal is None or owner is None or owner == terminal)), None)
            if gate is None:
                raise ValueError("not enough passenger gates for initially parked aircraft")
        else:
            raise ValueError("each initially parked aircraft requires a gate; regenerate the legacy scenario")
        if not gate_fits(gate, aircraft):
            raise ValueError("initial passenger gate has no matched IFATC capacity for this aircraft")
        if gate_blocker(gates, gate) is not None:
            raise ValueError("initial passenger gate is occupied more than once")
        plane = {field: aircraft[field] for field in AIRCRAFT_FIELDS}
        gates[gate] = plane
        parked.append({**plane, "gate": gate})
    return parked, gates
