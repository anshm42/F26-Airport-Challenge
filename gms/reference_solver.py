"""
Reference solver and scenario feasibility checker.

This is the backstop for the solvability guarantee: every scenario we ship must
have at least one assignment of all flights to gates with ZERO hard failures.
``scenario_feasible`` proves that for a schedule, and ``feasible`` is the
underlying constraint solver (interval-graph colouring with the asymmetric
compatibility rule, solved by most-constrained-first backtracking).

Reassignments are a soft cost, never a hard failure, so online solvability is
implied if every per-tick active flight set is statically assignable -- a solver
could, in the worst case, re-solve from scratch each tick using reassignments.
``scenario_feasible`` therefore checks the active set after every tick.
"""

from .compat import hard_compatible
from .occupancy import sets_conflict
from .messages import FlightStore, parse_gate_outage
from .profile import flight_profile
from .loader import load_static, load_schedule


def compatible_gates(profile, gates: dict) -> list[str]:
    out = []
    for gid, gate in gates.items():
        ok, _ = hard_compatible(profile.category, profile.wingspan,
                                profile.jetbridge_required, gate)
        if ok:
            out.append(gid)
    return out


def feasible(profiles, gates: dict, gate_outages: dict = None):
    """
    Return a {flight_id: gate_id} assignment with no hard failures, or None if
    none exists. Only flights with a real YYZ presence are placed. Outage
    windows pre-occupy their gate so flights can't be placed across them.
    """
    gate_outages = gate_outages or {}
    profiles = [p for p in profiles if p.has_yyz and p.intervals]

    options = {p.flight_id: compatible_gates(p, gates) for p in profiles}
    if any(not opts for opts in options.values()):
        return None

    # Most-constrained-first: fewest gate options, then earliest presence.
    order = sorted(profiles, key=lambda p: (len(options[p.flight_id]), p.earliest_start or 0))

    assignment: dict[str, str] = {}
    gate_intervals: dict[str, list] = {gid: list(gate_outages.get(gid, [])) for gid in gates}

    def backtrack(i: int) -> bool:
        if i == len(order):
            return True
        p = order[i]
        for gid in options[p.flight_id]:
            if not sets_conflict(gate_intervals[gid], p.intervals):
                assignment[p.flight_id] = gid
                gate_intervals[gid].extend(p.intervals)
                if backtrack(i + 1):
                    return True
                del gate_intervals[gid][-len(p.intervals):]
                del assignment[p.flight_id]
        return False

    return dict(assignment) if backtrack(0) else None


def scenario_feasible(static_path: str, schedule_path: str):
    """
    Return (True, None) if every tick's active flight set is assignable, else
    (False, minute_of_first_infeasible_tick).
    """
    gates, ac_info, stations = load_static(static_path)
    schedule = load_schedule(schedule_path)

    store = FlightStore()
    gate_outages = {gid: [] for gid in gates}
    for t in sorted(schedule):
        for msg in schedule[t]:
            if msg.get("action") == "GateOutage":
                gid, window = parse_gate_outage(msg)
                if gid in gate_outages:
                    gate_outages[gid].append(window)
            else:
                store.apply(msg, t)
        profiles = [flight_profile(s, ac_info, stations) for s in store.flights.values()]
        active = [p for p in profiles if p.has_yyz]
        if feasible(active, gates, gate_outages) is None:
            return False, t

    return True, None
