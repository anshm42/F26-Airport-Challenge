"""
Flight classification and gate compatibility.

Classification (per flight, from its YYZ-relevant legs):
  - cargo         : the aircraft is flagged as a freighter (overrides route)
  - international : any YYZ leg's other station is an international station
  - domestic      : otherwise

Compatibility is ASYMMETRIC (the realistic customs/immigration rule):
  - cargo flight         -> cargo gate only        (type 0)
  - international flight  -> international gate only (type 2)
  - domestic flight       -> domestic OR international gate (type 1 or 2)

Plus two hard physical constraints, checked against the most constraining
aircraft across the flight's YYZ legs:
  - wingspan must fit the gate
  - a jetbridge-required aircraft needs a jetbridge gate

A domestic flight placed at an international gate is allowed but earns a soft
"premium gate wasted" penalty (handled by the scorer, not here).
"""

from .config import HOME, CARGO, DOMESTIC, INTERNATIONAL


def is_cargo_aircraft(ac_record: dict) -> bool:
    return bool(ac_record.get("cargo", False))


def station_type(station: str, stations: dict) -> str:
    """'domestic' | 'international' | 'home' for a station code.

    An unrecognised station is treated as international: that is the safer,
    more realistic default (an unknown origin is assumed to need customs and
    therefore an international gate)."""
    if station == HOME:
        return "home"
    return stations.get(station, "international")


def classify(yyz_legs: list[dict], ac_info: dict, stations: dict) -> int:
    """Return CARGO / DOMESTIC / INTERNATIONAL for a flight's YYZ legs."""
    for leg in yyz_legs:
        ac = ac_info.get(leg["aircraftType"])
        if ac and is_cargo_aircraft(ac):
            return CARGO

    for leg in yyz_legs:
        other = leg["arrivalStation"] if leg["departureStation"] == HOME else leg["departureStation"]
        if station_type(other, stations) == "international":
            return INTERNATIONAL

    return DOMESTIC


def type_allowed(category: int, gate_type: int) -> bool:
    """Asymmetric gate-type rule."""
    if category == CARGO:
        return gate_type == CARGO
    if category == INTERNATIONAL:
        return gate_type == INTERNATIONAL
    # domestic may use a domestic or an international gate
    return gate_type in (DOMESTIC, INTERNATIONAL)


def hard_compatible(category: int, wingspan: float, jetbridge_required: bool, gate: dict):
    """
    Check the hard constraints. Returns (ok: bool, reason: str|None).
    ``reason`` is one of 'wingspan' | 'jetbridge' | 'type' when not ok.
    """
    if wingspan > gate["max_wingspan"]:
        return False, "wingspan"
    if jetbridge_required and not gate["jetbridge"]:
        return False, "jetbridge"
    if not type_allowed(category, gate["gate_type"]):
        return False, "type"
    return True, None


def is_premium_misuse(category: int, gate_type: int) -> bool:
    """Domestic flight occupying an international gate (allowed, soft-penalised)."""
    return category == DOMESTIC and gate_type == INTERNATIONAL
