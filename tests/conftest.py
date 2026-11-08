import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCENARIOS = [
    "simple", "cascade_1", "cascade_2",
    "cargo", "overnight", "equipment_upgrade", "cancel", "domestic_787",
    "gate_outage", "busy_day", "rush_hour", "emergencies",
]


@pytest.fixture
def static_path():
    return str(ROOT / "static_info.json")


@pytest.fixture
def static_data():
    from gms.loader import load_static
    return load_static(str(ROOT / "static_info.json"))


def scenario_path(name):
    return str(ROOT / "flight_data" / f"{name}.json")
