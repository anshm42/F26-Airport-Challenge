"""
Starter solution.

This is a CORRECT but minimal baseline: it assigns each waiting flight to the
first compatible, conflict-free gate, and moves an already-assigned flight only
when an airport update (a timing/equipment change, or a gate outage) has made
its current gate invalid. It avoids every hard failure but does not optimise
(walking distance, premium-gate use, etc.).

Improve `decide` to score better. The evaluator gives you, each tick:

    observation = {
        "time": <minute of day>,
        "gates": { gate_id: {gate_type, max_wingspan, dist, jetbridge} },
        "gate_outages": { gate_id: [ [start, end], ... ] },  # gates down for a window
        "waiting_flights":  { fid: flight_view },             # unassigned, need a gate
        "assigned_flights": { fid: flight_view + {gate_id} },
        "changed_flights":  { fid: {gate_id, reason, old, new} },  # revisit these
        "gate_assignments": { gate_id: [ {flight_id, intervals} ] },
        "ac_info": { ... },
    }

where flight_view = {flight_id, intervals, category, wingspan,
                     jetbridge_required, legs, info_time}.

category and gate_type both use: 0 = cargo, 1 = domestic, 2 = international.
Return {"assignments": [(fid, gid)], "reassignments": [(fid, gid)]}.
"""

CARGO, DOMESTIC, INTERNATIONAL = 0, 1, 2


def compatible(flight, gate) -> bool:
    """Hard constraints: size, jetbridge, asymmetric gate type."""
    if flight["wingspan"] > gate["max_wingspan"]:
        return False
    if flight["jetbridge_required"] and not gate["jetbridge"]:
        return False
    category, gate_type = flight["category"], gate["gate_type"]
    if category == CARGO:
        return gate_type == CARGO
    if category == INTERNATIONAL:
        return gate_type == INTERNATIONAL
    return gate_type in (DOMESTIC, INTERNATIONAL)  # domestic flight


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def gate_blocked(gid, intervals, local, outages, exclude=None) -> bool:
    """True if another flight or a gate outage occupies any of these intervals."""
    for booking in local.get(gid, []):
        if booking["flight_id"] == exclude:
            continue
        if any(_overlap(x, y) for x in intervals for y in booking["intervals"]):
            return True
    for window in outages.get(gid, []):
        if any(_overlap(x, window) for x in intervals):
            return True
    return False


def first_gate(flight, gates, local, outages, exclude=None, skip=None):
    intervals = flight["intervals"]
    for gid, gate in gates.items():
        if gid == skip:
            continue
        if compatible(flight, gate) and not gate_blocked(gid, intervals, local, outages, exclude):
            return gid
    return None


def decide(observation):
    gates = observation["gates"]
    outages = observation.get("gate_outages", {})
    assignments, reassignments = [], []

    # Local mutable copy of the gate plan so we don't double-book within this tick.
    local = {gid: list(bookings) for gid, bookings in observation["gate_assignments"].items()}

    # 1. Repair already-assigned flights whose gate is no longer valid
    #    (timing/equipment change, or an outage on their gate).
    for fid, info in observation["changed_flights"].items():
        cur = info["gate_id"]
        new = info["new"]
        flight = {
            "intervals": new["intervals"],
            "wingspan": new["wingspan"],
            "jetbridge_required": new["jetbridge_required"],
            "category": new["category"],
        }
        if compatible(flight, gates[cur]) and not gate_blocked(cur, flight["intervals"], local, outages, exclude=fid):
            continue  # still fine
        new_gid = first_gate(flight, gates, local, outages, exclude=fid, skip=cur)
        if new_gid is not None:
            reassignments.append((fid, new_gid))
            local[cur] = [b for b in local.get(cur, []) if b["flight_id"] != fid]
            local.setdefault(new_gid, []).append({"flight_id": fid, "intervals": flight["intervals"]})

    # 2. Assign waiting flights (emergencies / diversions first).
    waiting = sorted(observation["waiting_flights"].items(),
                     key=lambda kv: not kv[1].get("priority", False))
    for fid, flight in waiting:
        gid = first_gate(flight, gates, local, outages)
        if gid is not None:
            assignments.append((fid, gid))
            local.setdefault(gid, []).append({"flight_id": fid, "intervals": flight["intervals"]})

    return {"assignments": assignments, "reassignments": reassignments}
