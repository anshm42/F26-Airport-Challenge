import sys
import types

import pytest

from conftest import SCENARIOS, scenario_path
from evaluator import run_sim


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("solution", ["solution", "solution_kd"])
def test_reference_solutions_have_no_hard_failures(static_path, scenario, solution):
    result = run_sim(static_path, scenario_path(scenario), solution, verbose=False)
    assert result["status"] == "OK", f"{scenario}/{solution}: {result.get('reason')}"
    assert result["unassigned"] == []


def _inject(name, decide_fn):
    mod = types.ModuleType(name)
    mod.decide = decide_fn
    sys.modules[name] = mod
    return name


def test_double_booking_is_a_hard_failure(static_path):
    # Force every waiting flight onto one gate, ignoring conflicts.
    name = _inject("evil_doublebook",
                   lambda obs: {"assignments": [(fid, "I1") for fid in obs["waiting_flights"]],
                                "reassignments": []})
    result = run_sim(static_path, scenario_path("simple"), name, verbose=False)
    assert result["status"] == "FAILED"
    assert "blocked by flight" in result["reason"]   # flight-vs-flight double-booking


def test_incompatible_gate_is_a_hard_failure(static_path):
    # Send an international flight to a domestic gate (customs violation).
    def decide(obs):
        out = []
        for fid, f in obs["waiting_flights"].items():
            gid = "D1" if f["category"] == 2 else "I1"
            out.append((fid, gid))
        return {"assignments": out, "reassignments": []}
    name = _inject("evil_intl_on_domestic", decide)
    result = run_sim(static_path, scenario_path("overnight"), name, verbose=False)
    assert result["status"] == "FAILED"
    assert "type" in result["reason"] or "Incompatible" in result["reason"]


def test_cancel_frees_the_gate(static_path):
    # The reference solution must end with AC-201 cancelled (not assigned) and no failure.
    result = run_sim(static_path, scenario_path("cancel"), "solution", verbose=False)
    assert result["status"] == "OK"
    # AC-201 was cancelled mid-run; only AC-202 should remain assigned.
    assert result["assigned"] == 1


def test_domestic_787_uses_intl_gate_with_premium_penalty(static_path):
    result = run_sim(static_path, scenario_path("domestic_787"), "solution", verbose=False)
    assert result["status"] == "OK"
    assert "domestic_at_intl_gate" in result["breakdown"]


def test_equipment_upgrade_forces_reassignment(static_path):
    # 73G upgraded to a 787 mid-run; its small domestic gate no longer fits.
    result = run_sim(static_path, scenario_path("equipment_upgrade"), "solution", verbose=False)
    assert result["status"] == "OK"
    assert result["assigned"] == 1


def test_emergency_diversions_get_assigned(static_path):
    # 4 planned flights + a medical diversion + an air turnback = 6, all placed.
    result = run_sim(static_path, scenario_path("emergencies"), "solution_kd", verbose=False)
    assert result["status"] == "OK"
    assert result["unassigned"] == []
    assert result["assigned"] == 6


def test_unplaced_emergency_is_penalised(static_path):
    # A solution that assigns nothing leaves the diversions unplaced -> heavier penalty.
    name = _inject("noop_solution", lambda obs: {"assignments": [], "reassignments": []})
    result = run_sim(static_path, scenario_path("emergencies"), name, verbose=False)
    assert result["status"] == "OK"
    assert "unassigned_emergency" in result["breakdown"]


def test_gate_outage_ignored_is_a_hard_failure(static_path):
    # A solution that places flights but never reacts to outages/changes must
    # fail when an outage lands on the gate it used (the flight goes to D1 first).
    def decide(obs):
        gates, local, out = obs["gates"], {}, []
        for gid, bks in obs["gate_assignments"].items():
            local[gid] = list(bks)
        for fid, f in obs["waiting_flights"].items():
            for gid, g in gates.items():
                type_ok = (g["gate_type"] in (1, 2)) if f["category"] == 1 else (g["gate_type"] == f["category"])
                fits = f["wingspan"] <= g["max_wingspan"] and (not f["jetbridge_required"] or g["jetbridge"])
                clash = any(a[0] < b[1] and b[0] < a[1]
                            for x in local.get(gid, []) for a in f["intervals"] for b in x["intervals"])
                if type_ok and fits and not clash:
                    out.append((fid, gid))
                    local.setdefault(gid, []).append({"flight_id": fid, "intervals": f["intervals"]})
                    break
        return {"assignments": out, "reassignments": []}
    name = _inject("lazy_no_outage_handling", decide)
    result = run_sim(static_path, scenario_path("gate_outage"), name, verbose=False)
    assert result["status"] == "FAILED"
    assert "outage" in result["reason"].lower()
