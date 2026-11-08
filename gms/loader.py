"""
JSON loading for static airport info and flight schedules.

Kept separate from the evaluator so the reference solver / feasibility checker
can reuse it without importing the simulation loop.
"""

import json

from .timeutil import hhmm_key_to_min


def load_static(path: str):
    """Return (gates, aircraft_info, stations) from static_info.json."""
    with open(path, "r") as f:
        data = json.load(f)
    gates = data["gate_info"]
    aircraft_info = data["aircraft_info"]
    stations = data.get("stations", {})
    return gates, aircraft_info, stations


def load_schedule(path: str) -> dict:
    """Return {minute_of_day: [message, ...]} from a flight schedule file."""
    with open(path, "r") as f:
        raw = json.load(f)
    schedule = {}
    for time_key, messages in raw.items():
        schedule[hhmm_key_to_min(time_key)] = messages
    return schedule
