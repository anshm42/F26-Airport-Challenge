"""
Gate Management System -- public evaluator.

Single canonical evaluator for the gate-assignment subproblem. It:

  1. loads + validates the static airport info and a flight schedule,
  2. replays the schedule message-by-message (the live AODB feed),
  3. each tick, hands the solution an observation and applies its decisions,
  4. enforces hard rules and accumulates a weighted soft score,
  5. reports a final result.

Run it directly::

    python evaluator.py --scenario flight_data/simple.json --solution solution

Domain semantics live in the ``gms`` package; this file is orchestration only.
Output is ASCII so it never crashes on a non-UTF-8 Windows console.
"""

import argparse
import importlib
import sys

from gms.config import CATEGORY_NAME
from gms.timeutil import fmt
from gms.loader import load_static, load_schedule
from gms.messages import FlightStore, parse_gate_outage, OUTAGE_OPEN_END
from gms.profile import flight_profile
from gms.occupancy import sets_conflict, intervals_overlap
from gms.compat import hard_compatible, is_premium_misuse
from gms.scoring import ScoreCard, HardFailure


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _intervals_label(intervals) -> str:
    return ", ".join(f"[{fmt(s)}, {fmt(e)}]" for s, e in intervals) or "(no presence)"


def _blocked_reason(gid, fid, intervals, gate_assignments, gate_outages):
    """Why ``intervals`` can't occupy gid (excluding fid), or None if free."""
    for booking in gate_assignments.get(gid, []):
        if booking["flight_id"] == fid:
            continue
        if sets_conflict(intervals, booking["intervals"]):
            return f"flight {booking['flight_id']}"
    for window in gate_outages.get(gid, []):
        if any(intervals_overlap(iv, window) for iv in intervals):
            return f"a gate outage [{fmt(window[0])}, {fmt(window[1])}]"
    return None


def _remove_booking(gid, fid, gate_assignments):
    bookings = gate_assignments.get(gid)
    if not bookings:
        return
    gate_assignments[gid] = [b for b in bookings if b["flight_id"] != fid]


def _profile_changed(old, new) -> bool:
    return (
        old.intervals != new.intervals
        or old.category != new.category
        or old.wingspan != new.wingspan
        or old.jetbridge_required != new.jetbridge_required
    )


def _validate_actions(actions, t):
    if not isinstance(actions, dict):
        raise HardFailure("Solution.decide must return a dict.", t)
    for key in ("assignments", "reassignments"):
        seq = actions.get(key, [])
        if not isinstance(seq, (list, tuple)):
            raise HardFailure(f"'{key}' must be a list of (flight_id, gate_id) pairs.", t)
        for pair in seq:
            if (not isinstance(pair, (list, tuple))) or len(pair) != 2:
                raise HardFailure(f"Invalid {key} entry {pair!r}; expected (flight_id, gate_id).", t)
    return actions.get("assignments", []), actions.get("reassignments", [])


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def run_sim(static_info: str, flight_schedule: str, solution_module: str,
            verbose: bool = True, record: bool = False) -> dict:
    """Run a full simulation. Returns a result dict (also used by tests).

    When ``record`` is True the result includes a ``trace`` describing the gate
    plan after every tick, used by the visualiser (visualize.py)."""
    gates, ac_info, stations = load_static(static_info)
    schedule = load_schedule(flight_schedule)
    solution = importlib.import_module(solution_module)

    store = FlightStore()
    gate_assignments: dict[str, list] = {gid: [] for gid in gates}
    gate_outages: dict[str, list] = {gid: [] for gid in gates}
    flight_assignments: dict[str, dict] = {}   # fid -> {gate_id, snapshot}
    score = ScoreCard()
    trace = []
    tick_lines = []

    def log(msg):
        if verbose:
            print(msg)
        if record:
            tick_lines.append(msg)

    def profile_of(fid):
        return flight_profile(store.flights[fid], ac_info, stations)

    sorted_times = sorted(schedule.keys())
    log(f"Simulation: {len(sorted_times)} event(s) from {fmt(sorted_times[0])} "
        f"to {fmt(sorted_times[-1])}\n")

    try:
        for t in sorted_times:
            tick_lines = []
            messages = schedule[t]
            log(f"--- t={fmt(t)} | {len(messages)} message(s) ---")

            # 1) Apply messages (flight messages -> store; gate outages -> resource state)
            touched = []
            outaged_gates = set()
            for msg in messages:
                if msg.get("action") == "GateOutage":
                    gid, window = parse_gate_outage(msg)
                    if gid not in gates:
                        log(f"  [WARN] GateOutage for unknown gate {gid} (ignored).")
                        continue
                    gate_outages[gid].append(window)
                    outaged_gates.add(gid)
                    log(f"  [OUTAGE] Gate {gid} unavailable [{fmt(window[0])}, {fmt(window[1])}]")
                    continue
                fid, warn = store.apply(msg, t)
                if warn:
                    log(f"  [WARN] {warn}")
                if fid:
                    touched.append(fid)
                    if msg.get("action") == "DivertFlight":
                        log(f"  [DIVERT] unscheduled {store.flights[fid].get('reason','diversion')} "
                            f"arrival: {fid}")

            # 2) Free gates of assigned flights that were cancelled / lost YYZ legs
            for fid in list(flight_assignments):
                prof = profile_of(fid)
                if store.flights[fid].get("cancelled") or not prof.has_yyz:
                    gid = flight_assignments[fid]["gate_id"]
                    _remove_booking(gid, fid, gate_assignments)
                    del flight_assignments[fid]
                    log(f"  [CANCEL] {fid} released gate {gid}")

            # 3) Flag assigned flights that need attention this tick:
            #    (a) their own info changed, or (b) an outage hit their gate.
            flagged = {}
            reasons = {}
            for fid in touched:
                if fid not in flight_assignments:
                    continue
                new = profile_of(fid)
                if _profile_changed(flight_assignments[fid]["snapshot"], new):
                    flagged[fid] = new
                    reasons[fid] = "info"
            for fid, a in flight_assignments.items():
                if fid in flagged or a["gate_id"] not in outaged_gates:
                    continue
                prof = profile_of(fid)
                windows = gate_outages[a["gate_id"]]
                if any(intervals_overlap(iv, w) for iv in prof.intervals for w in windows):
                    flagged[fid] = prof
                    reasons[fid] = "outage"

            # 4) Build observation
            waiting = {}
            for fid, state in store.flights.items():
                if state.get("cancelled") or fid in flight_assignments:
                    continue
                p = flight_profile(state, ac_info, stations)
                if not p.has_yyz:
                    continue
                waiting[fid] = _flight_view(p, state)

            assigned = {}
            for fid, a in flight_assignments.items():
                p = profile_of(fid)
                view = _flight_view(p, store.flights[fid])
                view["gate_id"] = a["gate_id"]
                assigned[fid] = view

            changed = {}
            for fid, new in flagged.items():
                old = flight_assignments[fid]["snapshot"]
                changed[fid] = {
                    "flight_id": fid,
                    "gate_id": flight_assignments[fid]["gate_id"],
                    "reason": reasons[fid],
                    "old": {"intervals": old.intervals, "category": old.category,
                            "wingspan": old.wingspan, "jetbridge_required": old.jetbridge_required},
                    "new": {"intervals": new.intervals, "category": new.category,
                            "wingspan": new.wingspan, "jetbridge_required": new.jetbridge_required},
                }

            observation = {
                "time": t,
                "gates": {gid: {k: g[k] for k in ("gate_type", "max_wingspan", "dist", "jetbridge")}
                          for gid, g in gates.items()},
                "gate_outages": {gid: [list(w) for w in windows]
                                 for gid, windows in gate_outages.items() if windows},
                "waiting_flights": waiting,
                "assigned_flights": assigned,
                "changed_flights": changed,
                "gate_assignments": {gid: [{"flight_id": b["flight_id"], "intervals": list(b["intervals"])}
                                           for b in bookings]
                                     for gid, bookings in gate_assignments.items()},
                "ac_info": ac_info,
            }

            # 5) Ask the solution
            try:
                actions = solution.decide(observation)
            except Exception as exc:  # noqa: BLE001 - surface solution crashes as hard fail
                raise HardFailure(f"Solution raised {type(exc).__name__}: {exc}", t)

            assignments_out, reassignments_out = _validate_actions(actions, t)

            # 6) Reassignments first, then new assignments
            for fid, new_gid in reassignments_out:
                _apply_reassignment(fid, new_gid, t, gates, store, ac_info, stations,
                                    gate_assignments, gate_outages, flight_assignments, flagged, score, log)
            for fid, gid in assignments_out:
                _apply_assignment(fid, gid, t, gates, store, ac_info, stations,
                                  gate_assignments, gate_outages, flight_assignments, score, log)

            # 7) Post-decision check on flagged flights still assigned
            for fid, new in flagged.items():
                if fid not in flight_assignments:
                    continue
                gid = flight_assignments[fid]["gate_id"]
                ok, reason = hard_compatible(new.category, new.wingspan, new.jetbridge_required, gates[gid])
                if not ok:
                    raise HardFailure(
                        f"Flight {fid} info change makes gate {gid} invalid ({reason}). "
                        f"Solution must reassign it via the reassignments list.", t)
                blocker = _blocked_reason(gid, fid, new.intervals, gate_assignments, gate_outages)
                if blocker:
                    raise HardFailure(
                        f"Flight {fid} must move off gate {gid}: blocked by {blocker}. "
                        f"Solution must reassign it via the reassignments list.", t)
                # still valid -> update booking + snapshot
                for b in gate_assignments[gid]:
                    if b["flight_id"] == fid:
                        b["intervals"] = new.intervals
                        break
                flight_assignments[fid]["snapshot"] = new
                log(f"  [UPDATE] {fid} @ {gid} now {_intervals_label(new.intervals)} (still valid)")

            if record:
                entry = _snapshot_tick(t, tick_lines, flight_assignments, gate_outages)
                entry["queue"] = [
                    {"flight_id": fid, "category": fv["category"],
                     "start": min((iv[0] for iv in fv["intervals"]), default=None)}
                    for fid, fv in waiting.items()
                ]
                entry["decisions"] = {
                    "assignments": [[a, b] for a, b in assignments_out],
                    "reassignments": [[a, b] for a, b in reassignments_out],
                }
                trace.append(entry)

    except HardFailure as hf:
        result = _finish(score, gates, flight_assignments, store, ac_info, stations,
                         log, hard_failure=hf)
    else:
        result = _finish(score, gates, flight_assignments, store, ac_info, stations, log)

    if record:
        result["trace"] = _build_trace(flight_schedule, solution_module, gates, trace)
    return result


def _snapshot_tick(t, tick_lines, flight_assignments, gate_outages):
    """Capture the full gate plan after a tick for the visualiser."""
    plan = {}
    for fid, a in flight_assignments.items():
        snap = a["snapshot"]
        plan.setdefault(a["gate_id"], []).append({
            "flight_id": fid,
            "intervals": [[int(s), int(e)] for s, e in snap.intervals],
            "category": snap.category,
            "diversion": snap.diversion,
        })
    outages = {gid: [[int(s), int(e)] for s, e in ws]
               for gid, ws in gate_outages.items() if ws}
    return {"time": int(t), "time_label": fmt(t), "log": list(tick_lines),
            "plan": plan, "outages": outages}


def _build_trace(scenario_path, solution_module, gates, ticks):
    """Assemble the trace payload (gate metadata, ticks, operational time range)."""
    ordered = sorted(gates.items(), key=lambda kv: (kv[1]["gate_type"], kv[0]))
    gate_meta = [{
        "id": gid, "type": g["gate_type"], "type_name": CATEGORY_NAME[g["gate_type"]],
        "max_wingspan": g["max_wingspan"], "jetbridge": g["jetbridge"], "dist": g["dist"],
    } for gid, g in ordered]

    bounded = []
    for tk in ticks:
        for bookings in tk["plan"].values():
            for b in bookings:
                for s, e in b["intervals"]:
                    bounded += [s, e]
        for windows in tk["outages"].values():
            for s, e in windows:
                bounded += [v for v in (s, e) if v < OUTAGE_OPEN_END]
    tmin = min(bounded) if bounded else 0
    tmax = max(bounded) if bounded else 24 * 60

    return {"scenario": scenario_path, "solution": solution_module,
            "gates": gate_meta, "ticks": ticks,
            "time_min": tmin, "time_max": tmax, "outage_open_end": OUTAGE_OPEN_END}


def _flight_view(profile, state) -> dict:
    return {
        "flight_id": profile.flight_id,
        "intervals": profile.intervals,
        "category": profile.category,
        "wingspan": profile.wingspan,
        "jetbridge_required": profile.jetbridge_required,
        "legs": profile.yyz_legs,
        "info_time": state.get("info_time"),
        "priority": profile.diversion,     # True for an unscheduled emergency arrival
        "reason": profile.reason,           # e.g. "medical", "turnback"
    }


def _apply_assignment(fid, gid, t, gates, store, ac_info, stations,
                      gate_assignments, gate_outages, flight_assignments, score, log):
    if gid not in gates:
        raise HardFailure(f"Invalid gate id '{gid}' for flight {fid}.", t)
    if fid in flight_assignments:
        raise HardFailure(f"Flight {fid} is already assigned; use reassignments.", t)
    state = store.flights.get(fid)
    if state is None or state.get("cancelled"):
        raise HardFailure(f"Flight {fid} is unknown or cancelled; cannot assign.", t)

    p = flight_profile(state, ac_info, stations)
    if not p.has_yyz:
        raise HardFailure(f"Flight {fid} has no YYZ presence; cannot assign.", t)

    gate = gates[gid]
    ok, reason = hard_compatible(p.category, p.wingspan, p.jetbridge_required, gate)
    if not ok:
        raise HardFailure(
            f"Incompatible assignment ({reason}): {fid} "
            f"({CATEGORY_NAME[p.category]}, {p.wingspan}m, jb={p.jetbridge_required}) -> "
            f"gate {gid} (type {gate['gate_type']}, max {gate['max_wingspan']}m, jb={gate['jetbridge']}).", t)

    blocker = _blocked_reason(gid, fid, p.intervals, gate_assignments, gate_outages)
    if blocker:
        raise HardFailure(f"Gate {gid} unavailable for {fid}: blocked by {blocker}.", t)

    gate_assignments[gid].append({"flight_id": fid, "intervals": p.intervals})
    flight_assignments[fid] = {"gate_id": gid, "snapshot": p}
    log(f"  [ASSIGN] {fid} -> {gid}  {_intervals_label(p.intervals)}")


def _apply_reassignment(fid, new_gid, t, gates, store, ac_info, stations,
                        gate_assignments, gate_outages, flight_assignments, flagged, score, log):
    if fid not in flight_assignments:
        raise HardFailure(f"Cannot reassign {fid}: not currently assigned.", t)
    if new_gid not in gates:
        raise HardFailure(f"Invalid gate id '{new_gid}' in reassignment of {fid}.", t)

    old_gid = flight_assignments[fid]["gate_id"]
    if new_gid == old_gid:
        return  # no-op, no penalty

    state = store.flights.get(fid)
    if state is None or state.get("cancelled"):
        raise HardFailure(f"Cannot reassign {fid}: unknown or cancelled.", t)

    p = flight_profile(state, ac_info, stations)
    gate = gates[new_gid]
    ok, reason = hard_compatible(p.category, p.wingspan, p.jetbridge_required, gate)
    if not ok:
        raise HardFailure(f"Incompatible reassignment ({reason}): {fid} -> gate {new_gid}.", t)

    blocker = _blocked_reason(new_gid, fid, p.intervals, gate_assignments, gate_outages)
    if blocker:
        raise HardFailure(f"Reassignment of {fid} -> {new_gid} blocked by {blocker}.", t)

    _remove_booking(old_gid, fid, gate_assignments)
    gate_assignments[new_gid].append({"flight_id": fid, "intervals": p.intervals})
    flight_assignments[fid] = {"gate_id": new_gid, "snapshot": p}

    # Soft cost. A move forced by the airport's own change (timing/equipment/outage)
    # is not the solution's fault, so only after-arrival moves are charged in that case.
    after_arrival = p.earliest_start is not None and p.earliest_start <= t
    if after_arrival:
        score.add("reassignment_after_arrival", f"{fid}: {old_gid}->{new_gid}", t)
    elif fid not in flagged:
        score.add("reassignment", f"{fid}: {old_gid}->{new_gid}", t)
    log(f"  [REASSIGN] {fid}: {old_gid} -> {new_gid}  {_intervals_label(p.intervals)}")


def _finish(score, gates, flight_assignments, store, ac_info, stations, log,
            hard_failure: HardFailure = None):
    # Standing soft costs over the final assignment: walking distance + premium misuse.
    for fid, a in flight_assignments.items():
        gid = a["gate_id"]
        gate = gates[gid]
        dist = gate.get("dist", 0)
        if isinstance(dist, (int, float)) and dist > 0:
            score.add("walking_distance_per_m", f"{fid} @ {gid}", amount=dist)
        snap = a["snapshot"]
        if is_premium_misuse(snap.category, gate["gate_type"]):
            score.add("domestic_at_intl_gate", f"{fid} @ {gid}")

    unassigned = [
        fid for fid, state in store.flights.items()
        if fid not in flight_assignments and not state.get("cancelled")
        and flight_profile(state, ac_info, stations).has_yyz
    ]
    for fid in unassigned:
        if store.flights[fid].get("diversion"):
            score.add("unassigned_emergency", fid)
        else:
            score.add("unassigned_flight", fid)

    log("\n" + "=" * 64)
    if hard_failure is not None:
        log("RESULT: HARD FAILURE")
        log(f"  Reason: {hard_failure.reason}")
        log(f"  Time:   {fmt(hard_failure.time)}")
        log("=" * 64)
        return {"status": "FAILED", "reason": hard_failure.reason,
                "time": hard_failure.time, "score": None,
                "assigned": len(flight_assignments), "unassigned": unassigned,
                "breakdown": score.breakdown()}

    breakdown = score.breakdown()
    log("RESULT: COMPLETED")
    log(f"  Flights assigned: {len(flight_assignments)} | unassigned: {len(unassigned)}")
    log(f"  Soft score (lower is better): {score.total:.2f}")
    for cat, info in sorted(breakdown.items()):
        log(f"    - {cat}: {info['count']}x = {info['points']:.2f}")
    if unassigned:
        log("  Unassigned flights:")
        for fid in unassigned:
            log(f"    - {fid}")
    log("=" * 64)
    return {"status": "OK", "reason": None, "time": None, "score": score.total,
            "assigned": len(flight_assignments), "unassigned": unassigned,
            "breakdown": breakdown}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="GMS gate-assignment evaluator")
    parser.add_argument("--scenario", default="flight_data/simple.json",
                        help="path to a flight schedule JSON")
    parser.add_argument("--static", default="static_info.json",
                        help="path to static_info.json")
    parser.add_argument("--solution", default="solution",
                        help="python module exposing decide(observation)")
    args = parser.parse_args(argv)

    result = run_sim(args.static, args.scenario, args.solution, verbose=True)
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
