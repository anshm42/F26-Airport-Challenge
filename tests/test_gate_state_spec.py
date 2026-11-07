import json
import unittest
from pathlib import Path


class GateStateSpecTest(unittest.TestCase):
    def test_example_has_every_passenger_gate_and_h_position_once(self):
        root = Path(__file__).resolve().parents[1]
        airport = json.loads((root / "data/pearson.json").read_text())
        request = json.loads((root / "docs/examples/gate-state-request.json").read_text())
        expected = {group[0] for terminal in airport["passengerGateGroups"]["terminals"].values()
                    for group in terminal["groups"]}
        expected.update(stand["name"] for stand in airport["ifatcStands"]["records"]
                        if stand["name"].startswith("Remote Stand H"))
        gates = request["gateState"]
        self.assertEqual(set(gates), expected)
        self.assertEqual(len(gates), 121)
        self.assertIn("D32", gates)
        self.assertNotIn("F32", gates)
        self.assertEqual(request["unassignedAircraft"], [])
        occupied = [plane for plane in gates.values() if plane is not None]
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0], {"carrier": "AC", "registration": "C-FFGZ", "aircraftType": "321"})


if __name__ == "__main__":
    unittest.main()
