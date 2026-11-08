"""
Per-presence gate occupancy.

A flight occupies its gate for each contiguous stretch it is physically at YYZ.
Walking the legs in order:

  - arrival at YYZ                -> a presence starts
  - departure from YYZ, with an
    open presence                 -> close it: [arrival, departure]  (turnaround)
  - departure from YYZ, with no
    open presence                 -> [departure - PREP, departure]   (originating)
  - end with an open presence     -> [arrival, arrival + GROUND]      (inbound-only)

A flight that departs YYZ and later returns therefore yields TWO intervals, with
the gate correctly FREE in between (the old single-window model got this wrong
and inverted the window, hiding real conflicts).

Intervals are half-open [start, end): exact back-to-back use of a gate does not
conflict. An optional buffer enforces minimum separation between aircraft.
"""

from .config import HOME, PREP_MINUTES, GROUND_MINUTES, TURNAROUND_BUFFER

# An interval is a (start_min, end_min) tuple of absolute minutes.
Interval = tuple


def build_presences(active_legs: list[dict],
                    prep: int = PREP_MINUTES,
                    ground: int = GROUND_MINUTES) -> list[Interval]:
    """
    Build the list of YYZ presence intervals from a flight's ordered active legs.

    Each leg dict must carry absolute minutes in ``dep_abs`` / ``arr_abs`` and
    string station codes in ``departureStation`` / ``arrivalStation``.
    Non-YYZ legs simply contribute nothing.
    """
    intervals: list[Interval] = []
    at_yyz_since = None

    for leg in active_legs:
        if leg["arrivalStation"] == HOME:
            at_yyz_since = leg["arr_abs"]
        if leg["departureStation"] == HOME:
            dep = leg["dep_abs"]
            if at_yyz_since is not None:
                intervals.append((at_yyz_since, dep))
                at_yyz_since = None
            else:
                intervals.append((dep - prep, dep))

    if at_yyz_since is not None:
        intervals.append((at_yyz_since, at_yyz_since + ground))

    return intervals


def intervals_overlap(a: Interval, b: Interval, buffer: int = TURNAROUND_BUFFER) -> bool:
    """Half-open overlap with an optional separation buffer."""
    return a[0] < b[1] + buffer and b[0] < a[1] + buffer


def sets_conflict(a: list[Interval], b: list[Interval],
                  buffer: int = TURNAROUND_BUFFER) -> bool:
    """True if any interval in ``a`` overlaps any interval in ``b``."""
    return any(intervals_overlap(x, y, buffer) for x in a for y in b)


def earliest_start(intervals: list[Interval]):
    """Earliest presence start, or None if the flight has no YYZ presence."""
    return min((s for s, _ in intervals), default=None)
