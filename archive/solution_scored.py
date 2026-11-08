"""
Reference solution (a cleaner, scoring-aware example).

Same hard-constraint handling as the starter (including gate outages), but
instead of the first compatible gate it picks the *best* one by a small cost
function:
  - keep domestic flights off international gates when possible (avoid the
    premium-gate penalty),
  - prefer a tighter wingspan fit,
  - prefer a shorter walking distance,
  - don't waste a jetbridge on a flight that doesn't need one.

This is a greedy heuristic, not an optimum, but it is correct and scores well
on the sample scenarios.
"""

CARGO, DOMESTIC, INTERNATIONAL = 0, 1, 2


def compatible(flight, gate) -> bool:
    if flight["wingspan"] > gate["max_wingspan"]:
        return False
    if flight["jetbridge_required"] and not gate["jetbridge"]:
        return False
    category, gate_type = flight["category"], gate["gate_type"]
    if category == CARGO:
        return gate_type == CARGO
    if category == INTERNATIONAL:
        return gate_type == INTERNATIONAL
    return gate_type in (DOMESTIC, INTERNATIONAL)


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def gate_blocked(gid, intervals, local, outages, exclude=None) -> bool:
    for booking in local.get(gid, []):
        if booking["flight_id"] == exclude:
            continue
        if any(_overlap(x, y) for x in intervals for y in booking["intervals"]):
            return True
    for window in outages.get(gid, []):
        if any(_overlap(x, window) for x in intervals):
            return True
    return False


def gate_cost(flight, gate) -> float:
    """Lower is better."""
    cost = 0.0
    if flight["category"] == DOMESTIC and gate["gate_type"] == INTERNATIONAL:
        cost += 1000   # avoid premium gates for domestic flights
    if not flight["jetbridge_required"] and gate["jetbridge"]:
        cost += 100    # don't waste a jetbridge
    cost += max(0, gate["max_wingspan"] - flight["wingspan"])   # tight fit
    cost += max(0, gate.get("dist", 0))                          # short walk
    return cost


def best_gate(flight, gates, local, outages, exclude=None, skip=None):
    best_gid, best = None, None
    intervals = flight["intervals"]
    for gid, gate in gates.items():
        if gid == skip or not compatible(flight, gate):
            continue
        if gate_blocked(gid, intervals, local, outages, exclude):
            continue
        c = gate_cost(flight, gate)
        if best is None or c < best:
            best, best_gid = c, gid
    return best_gid


def decide(observation):
    gates = observation["gates"]
    outages = observation.get("gate_outages", {})
    assignments, reassignments = [], []
    local = {gid: list(bookings) for gid, bookings in observation["gate_assignments"].items()}

    # 1. Repair changed flights (timing/equipment change or gate outage).
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
            continue
        new_gid = best_gate(flight, gates, local, outages, exclude=fid, skip=cur)
        if new_gid is not None:
            reassignments.append((fid, new_gid))
            local[cur] = [b for b in local.get(cur, []) if b["flight_id"] != fid]
            local.setdefault(new_gid, []).append({"flight_id": fid, "intervals": flight["intervals"]})

    # 2. Assign waiting flights: emergencies first, then most-constrained (fewest legal gates).
    def options(flight):
        return sum(1 for g in gates.values() if compatible(flight, g))

    waiting = sorted(observation["waiting_flights"].items(),
                     key=lambda kv: (not kv[1].get("priority", False), options(kv[1])))
    for fid, flight in waiting:
        gid = best_gate(flight, gates, local, outages)
        if gid is not None:
            assignments.append((fid, gid))
            local.setdefault(gid, []).append({"flight_id": fid, "intervals": flight["intervals"]})

    return {"assignments": assignments, "reassignments": reassignments}
