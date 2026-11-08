"""
Time helpers.

The challenge runs over a single conceptual operating day. Times are kept as
integer "minutes since the start of day 0". Flights whose legs cross midnight
are handled by *absolutizing* their ordered event times: each successive time
that would go backwards is rolled forward by a day. This guarantees every
gate-occupancy window is monotonic and can never invert (the root cause of the
old double-booking bug).

``originatingDate`` is intentionally NOT used for the timeline: the sample data
is not date-coherent, and the simulation is a single operating period. The date
is used only for the flight identifier.
"""

MINUTES_PER_DAY = 24 * 60


def hhmm_str_to_min(value: str) -> int:
    """'HH:MM' -> minutes of day. e.g. '14:30' -> 870."""
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def hhmm_key_to_min(value) -> int:
    """Schedule key 'HHMM' (or int) -> minutes of day. e.g. '0900' -> 540."""
    n = int(value)
    return (n // 100) * 60 + (n % 100)


def absolutize(times_of_day: list[int]) -> list[int]:
    """
    Make an ordered list of minute-of-day values monotonically non-decreasing
    by rolling each backwards step forward a whole day.

    Feed a flight's event times in chronological order (dep0, arr0, dep1, ...).
    """
    out: list[int] = []
    base = 0
    prev = None
    for tod in times_of_day:
        t = tod + base
        if prev is not None and t < prev:
            base += MINUTES_PER_DAY
            t = tod + base
        out.append(t)
        prev = t
    return out


def fmt(minute) -> str:
    """Format an absolute minute as 'HH:MM' (or 'HH:MM+Nd' past midnight)."""
    if minute is None:
        return "N/A"
    minute = int(round(minute))
    day = minute // MINUTES_PER_DAY
    mod = minute % MINUTES_PER_DAY
    base = f"{mod // 60:02d}:{mod % 60:02d}"
    return f"{base}+{day}d" if day else base
