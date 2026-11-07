"""Validate transport fixtures and the existing flight-message contract."""

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .inventory import AIRCRAFT_FIELDS, canonical_gate, catalog, initial_gate_state, passenger_aircraft


ACTIONS = {
    "CreateFlight", "ReplaceFlight", "UpdateTiming", "CancelFlight",
    "ReinstateFlight", "UpdateEquipment", "GateOutage", "DivertFlight", "InitializeAirport",
}
COMPLETE_ACTIONS = {"CreateFlight", "ReplaceFlight", "UpdateTiming", "DivertFlight"}
DEFAULT_RATE = 60


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive_number(value, name):
    require(type(value) in (int, float), f"{name} must be a finite positive number")
    try:
        value = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite positive number") from error
    require(math.isfinite(value) and value > 0, f"{name} must be a finite positive number")
    return value


def object_field(parent, key):
    value = parent.get(key)
    require(isinstance(value, dict), f"{key} must be an object")
    return value


def string_field(parent, key, pattern):
    value = parent.get(key)
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None,
            f"invalid {key}")
    return value


def parse_utc(value):
    require(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)", value) is not None,
        "timestamp must be a UTC ISO 8601 datetime (Z or +00:00)")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def validate_aircraft(aircraft):
    require(isinstance(aircraft, dict), "aircraft must be an object")
    string_field(aircraft, "carrier", r"[A-Z0-9]{2}")
    string_field(aircraft, "registration", r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
    string_field(aircraft, "aircraftType", r"[A-Z0-9]{3}")
    require(passenger_aircraft(aircraft), "aircraft type must be a configured commercial passenger type")


def validate_leg_reference(leg):
    require(isinstance(leg, dict), "each leg must be an object")
    string_field(leg, "flightNumber", r"[0-9]{1,4}")
    if leg.get("flightSuffix") is not None:
        string_field(leg, "flightSuffix", r"[A-Za-z]")
    date.fromisoformat(string_field(leg, "originatingDate", r"[0-9]{4}-[0-9]{2}-[0-9]{2}"))
    string_field(leg, "departureStation", r"[A-Z]{3}")
    string_field(leg, "arrivalStation", r"[A-Z]{3}")


def flight_key(carrier, leg):
    return (carrier, leg["flightNumber"], leg.get("flightSuffix"), leg["originatingDate"],
            leg["departureStation"], leg["arrivalStation"])


def validate_gate_state(gates):
    require(isinstance(gates, dict) and set(gates) == set(catalog()["gates"]),
            "gateState must contain every canonical passenger gate and H position")
    registrations, footprints = set(), set()
    for gate, aircraft in gates.items():
        if aircraft is None:
            continue
        validate_aircraft(aircraft)
        require(set(aircraft) == set(AIRCRAFT_FIELDS), "gateState occupants contain only aircraft identity fields")
        require(aircraft["registration"] not in registrations, "aircraft occupies more than one gate")
        registrations.add(aircraft["registration"])
        footprint = catalog()["footprints"][gate]
        require(footprint not in footprints, "overlapping H positions cannot be occupied simultaneously")
        footprints.add(footprint)


def validate_message(message):
    require(isinstance(message, dict), "message must be an object")
    action = message.get("action")
    require(isinstance(action, str) and action in ACTIONS, "unsupported action")
    if "gateState" in message:
        validate_gate_state(message["gateState"])
    if action == "InitializeAirport":
        aircraft, flights, gates = {}, set(), set()
        for field, expected in (("flights", "CreateFlight"), ("gateOutages", "GateOutage")):
            items = message.get(field)
            require(isinstance(items, list), f"{field} must be an array")
            for item in items:
                require(isinstance(item, dict) and item.get("action") == expected,
                        f"{field} must contain {expected} messages")
                validate_message(item)
                if expected == "CreateFlight":
                    flight = item["flight"]
                    identity = (flight["carrier"], flight["aircraftType"])
                    require(aircraft.setdefault(flight["registration"], identity) == identity,
                            "conflicting initial aircraft details")
                    leg = item["legs"][0]
                    key = (flight["carrier"], leg["flightNumber"], leg.get("flightSuffix"), leg["originatingDate"])
                    require(key not in flights, "duplicate initial flight")
                    flights.add(key)
                else:
                    require(item["gate"] not in gates, "duplicate initial gate outage")
                    gates.add(item["gate"])
        parked = message.get("parkedAircraft")
        require(isinstance(parked, list), "parkedAircraft must be an array")
        registrations = set()
        for flight in parked:
            validate_aircraft(flight)
            tail = flight["registration"]
            require(tail not in registrations, "duplicate parked aircraft")
            registrations.add(tail)
            identity = (flight["carrier"], flight["aircraftType"])
            require(aircraft.get(tail, identity) == identity, "conflicting initial aircraft details")
        _, expected_gates = initial_gate_state(parked)
        if "gateState" in message:
            require(message["gateState"] == expected_gates, "initial gateState must match parkedAircraft exactly")
        return
    if action == "GateOutage":
        require("flight" not in message and "legs" not in message,
                "GateOutage must not contain flight or legs")
        require("emergencyState" not in message, "GateOutage must not contain emergencyState")
        string_field(message, "gate", r"\S+")
        string_field(message, "scheduledStartTime", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
        if "scheduledEndTime" in message:
            string_field(message, "scheduledEndTime", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
        return

    emergency_state = message.get("emergencyState")
    if "emergencyState" in message:
        require(isinstance(emergency_state, str) and emergency_state in ("Turnback", "Emergency"),
                "invalid emergencyState")
    flight = object_field(message, "flight")
    validate_aircraft(flight)
    require("gate" not in message and "gate" not in flight,
            "initial gates belong only to parkedAircraft, not scheduled flights")
    legs = message.get("legs")
    require(isinstance(legs, list) and len(legs) > 0, "legs must be a nonempty array")
    complete = action in COMPLETE_ACTIONS
    if not complete or action == "DivertFlight":
        require(len(legs) == 1, f"{action} requires exactly one leg")
    for index, leg in enumerate(legs):
        validate_leg_reference(leg)
        require("gate" not in leg, "scheduled legs cannot carry an initial gate")
        if complete:
            require(leg.get("legType") == ("Originating" if index == 0 else "Through"),
                    "complete legs must start with Originating, followed by Through")
            for key in ("scheduledDepartureTime", "scheduledArrivalTime"):
                string_field(leg, key, r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
            if index:
                require(legs[index - 1]["arrivalStation"] == leg["departureStation"],
                        "successive legs must connect")
        else:
            for key in ("scheduledDepartureTime", "scheduledArrivalTime"):
                if key in leg:
                    string_field(leg, key, r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
            if "legType" in leg:
                require(leg["legType"] in ("Originating", "Through"), "invalid legType")
        codeshares = leg.get("codeshare", [])
        require(isinstance(codeshares, list), "codeshare must be an array")
        for codeshare in codeshares:
            require(isinstance(codeshare, dict), "each codeshare must be an object")
            string_field(codeshare, "carrier", r"[A-Z0-9]{2}")
            string_field(codeshare, "flightNumber", r"[0-9]{1,4}")
    if action == "CancelFlight":
        require(object_field(message, "cancelScope").get("type") in ("AllLegs", "SpecificLeg"),
                "invalid cancelScope.type")
    if action == "UpdateEquipment":
        require(object_field(message, "equipmentUpdate").get("scope") in ("AllLegs", "SpecificLeg"),
                "invalid equipmentUpdate.scope")
    if action == "DivertFlight":
        require(legs[0]["arrivalStation"] == "YYZ", "diverted flight must arrive at YYZ")
        if "reason" in message:
            require(isinstance(message["reason"], str), "reason must be a string")
        if emergency_state == "Turnback" or ("emergencyState" not in message and message.get("reason") == "turnback"):
            require(legs[0]["departureStation"] == "YYZ", "turnback must depart from YYZ")


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(text):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, f"duplicate JSON field: {key}")
            value[key] = item
        return value
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def validate_scenario(scenario):
    require(isinstance(scenario, dict), "scenario must be an object")
    if "metadata" in scenario:
        metadata = object_field(scenario, "metadata")
        if "rate" in metadata:
            positive_number(metadata["rate"], "metadata.rate")
    events = scenario.get("events")
    require(isinstance(events, list) and len(events) > 0, "events must be a nonempty array")
    previous = None
    for index, event in enumerate(events):
        try:
            require(isinstance(event, dict), "event must be an object")
            timestamp = parse_utc(event.get("timestamp"))
            require(previous is None or timestamp >= previous, "timestamps must not decrease")
            validate_message(event.get("message"))
            require(index == 0 or event["message"]["action"] != "InitializeAirport",
                    "InitializeAirport must be the first and only initialization")
            previous = timestamp
        except ValueError as error:
            raise ValueError(f"event {index}: {error}") from error
    # Also reject non-JSON values in optional extension fields.
    json.dumps(scenario, allow_nan=False)


def load_scenario(path):
    scenario = read_json(Path(path).read_text(encoding="utf-8"))
    validate_scenario(scenario)
    return scenario
