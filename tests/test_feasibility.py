import json
import os
import tempfile

import pytest

from conftest import SCENARIOS, scenario_path
from gms.reference_solver import scenario_feasible
from scripts.validation import validate_static_info_json, validate_flight_schedule_json


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_shipped_scenario_is_solvable(static_path, scenario):
    ok, bad_tick = scenario_feasible(static_path, scenario_path(scenario))
    assert ok, f"{scenario} is infeasible at tick {bad_tick}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_shipped_scenario_validates(static_path, scenario):
    assert validate_static_info_json(static_path) == 1
    assert validate_flight_schedule_json(scenario_path(scenario)) == 1


def test_validation_rejects_a_corrupt_gate(static_path):
    # Regression for the old no-op validator (it keyed on 'gates' not 'gate_info').
    with open(static_path) as f:
        data = json.load(f)
    data["gate_info"]["BAD"] = {"gate_type": 9, "max_wingspan": -3, "dist": -99, "jetbridge": 7}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        assert validate_static_info_json(path) == 0
    finally:
        os.remove(path)
