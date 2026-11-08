"""
Per-flight profile: turn raw flight state (legs + cancellation info) into the
normalized facts the evaluator and solutions need.

A FlightProfile is what actually drives gate decisions:
  - intervals          : YYZ presence intervals (absolute minutes)
  - category           : CARGO / DOMESTIC / INTERNATIONAL
  - wingspan           : most constraining wingspan across YYZ legs
  - jetbridge_required : True if any YYZ leg needs a jetbridge
  - active_legs        : non-cancelled legs with absolute minutes attached
"""

from dataclasses import dataclass, field

from .config import HOME
from .timeutil import hhmm_str_to_min, absolutize
from .occupancy import build_presences, earliest_start
from .compat import classify


@dataclass
class FlightProfile:
    flight_id: str
    intervals: list = field(default_factory=list)
    category: int = None
    wingspan: float = 0.0
    jetbridge_required: bool = False
    active_legs: list = field(default_factory=list)
    yyz_legs: list = field(default_factory=list)
    diversion: bool = False
    reason: str = None

    @property
    def has_yyz(self) -> bool:
        return bool(self.yyz_legs)

    @property
    def earliest_start(self):
        return earliest_start(self.intervals)


def _active_legs(state: dict) -> list[dict]:
    cancelled = state.get("cancelled_legs", set())
    return [
        leg for leg in state["legs"]
        if (leg["departureStation"], leg["arrivalStation"]) not in cancelled
    ]


def flight_profile(state: dict, ac_info: dict, stations: dict) -> FlightProfile:
    """Compute the FlightProfile for a flight state. Always returns an object;
    ``has_yyz`` is False when the flight does not touch YYZ (and intervals are
    empty)."""
    fid = state["flight_id"]

    if state.get("cancelled"):
        return FlightProfile(flight_id=fid)

    active = [dict(leg) for leg in _active_legs(state)]
    if not active:
        return FlightProfile(flight_id=fid)

    # Absolute, monotonic minutes across the whole leg sequence (handles overnight).
    events = []
    for leg in active:
        events.append(hhmm_str_to_min(leg["scheduledDepartureTime"]))
        events.append(hhmm_str_to_min(leg["scheduledArrivalTime"]))
    abs_times = absolutize(events)
    for i, leg in enumerate(active):
        leg["dep_abs"] = abs_times[2 * i]
        leg["arr_abs"] = abs_times[2 * i + 1]

    yyz_legs = [leg for leg in active
                if leg["departureStation"] == HOME or leg["arrivalStation"] == HOME]

    if not yyz_legs:
        return FlightProfile(flight_id=fid, active_legs=active)

    intervals = build_presences(active)
    category = classify(yyz_legs, ac_info, stations)
    wingspan = max(float(ac_info[leg["aircraftType"]]["wingspan"]) for leg in yyz_legs)
    jetbridge_required = any(
        bool(ac_info[leg["aircraftType"]].get("jetbridge_required", 0)) for leg in yyz_legs
    )

    return FlightProfile(
        flight_id=fid,
        intervals=intervals,
        category=category,
        wingspan=wingspan,
        jetbridge_required=jetbridge_required,
        active_legs=active,
        yyz_legs=yyz_legs,
        diversion=bool(state.get("diversion")),
        reason=state.get("reason"),
    )
