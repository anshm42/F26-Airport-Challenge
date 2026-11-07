"""Validate transport fixtures and the existing flight-message contract."""

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path


ACTIONS = {
    "CreateFlight", "ReplaceFlight", "UpdateTiming", "CancelFlight",
    "ReinstateFlight", "UpdateEquipment", "GateOutage", "DivertFlight",
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


def validate_message(message):
    require(isinstance(message, dict), "message must be an object")
    action = message.get("action")
    require(isinstance(action, str) and action in ACTIONS, "unsupported action")
    if action == "GateOutage":
        require("flight" not in message and "legs" not in message,
                "GateOutage must not contain flight or legs")
        string_field(message, "gate", r"\S+")
        string_field(message, "scheduledStartTime", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
        if "scheduledEndTime" in message:
            string_field(message, "scheduledEndTime", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
        return

    flight = object_field(message, "flight")
    string_field(flight, "carrier", r"[A-Z0-9]{2}")
    string_field(flight, "registration", r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
    string_field(flight, "aircraftType", r"[A-Z0-9]{3}")
    legs = message.get("legs")
    require(isinstance(legs, list) and len(legs) > 0, "legs must be a nonempty array")
    complete = action in COMPLETE_ACTIONS
    if not complete or action == "DivertFlight":
        require(len(legs) == 1, f"{action} requires exactly one leg")
    for index, leg in enumerate(legs):
        require(isinstance(leg, dict), "each leg must be an object")
        string_field(leg, "flightNumber", r"[0-9]{1,4}")
        if leg.get("flightSuffix") is not None:
            string_field(leg, "flightSuffix", r"[A-Za-z]")
        date.fromisoformat(string_field(leg, "originatingDate", r"[0-9]{4}-[0-9]{2}-[0-9]{2}"))
        string_field(leg, "departureStation", r"[A-Z]{3}")
        string_field(leg, "arrivalStation", r"[A-Z]{3}")
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


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(text):
    return json.loads(text, parse_constant=reject_constant)


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
            previous = timestamp
        except ValueError as error:
            raise ValueError(f"event {index}: {error}") from error
    # Also reject non-JSON values in optional extension fields.
    json.dumps(scenario, allow_nan=False)


def load_scenario(path):
    scenario = read_json(Path(path).read_text(encoding="utf-8"))
    validate_scenario(scenario)
    return scenario
