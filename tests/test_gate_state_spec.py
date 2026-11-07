import json
import unittest
from pathlib import Path


class GateStateSpecTest(unittest.TestCase):
    def test_example_has_every_passenger_gate_and_h_position_once(self):
        root = Path(__file__).resolve().parents[1]
        airport = json.loads((root / "data/pearson.json").read_text())
        request = json.loads((root / "docs/examples/gate-state-request.json").read_text())
        expected = {row["gate"] for row in airport["passengerStandMapping"]["records"]}
        gates = request["gateState"]
        self.assertEqual(set(gates), expected)
        self.assertEqual(len(gates), 110)
        self.assertIn("D32", gates)
        self.assertNotIn("F32", gates)
        self.assertEqual(request["unassignedAircraft"], [])
        occupied = [plane for plane in gates.values() if plane is not None]
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0], {"carrier": "AC", "registration": "C-FFGZ", "aircraftType": "321"})

    def test_crosswalk_references_exact_ifatc_records_and_preserves_swing_groups(self):
        root = Path(__file__).resolve().parents[1]
        airport = json.loads((root / "data/pearson.json").read_text())
        stands = {row["name"]: row for row in airport["ifatcStands"]["records"]}
        mapping = airport["passengerStandMapping"]
        rows = {row["gate"]: row for row in mapping["records"]}
        self.assertEqual(len(rows), len(mapping["records"]))
        excluded = set(mapping["excludedPassengerGates"])
        self.assertEqual(excluded, {"D30", "F82", "F83", "E82", "E85", "B2a", "B2c", "B3", "B4", "B5", "B23"})
        terminal_ids = {group[0] for terminal in airport["passengerGateGroups"]["terminals"].values()
                        for group in terminal["groups"]}
        remote_ids = {name for name in stands if name.startswith("Remote Stand H")}
        self.assertEqual(set(rows), (terminal_ids - excluded) | remote_ids)
        referenced = []
        for row in rows.values():
            self.assertTrue(row["ifatcStandNames"])
            self.assertEqual(row["widthClass"], max(stands[name]["widthClass"] for name in row["ifatcStandNames"]))
            self.assertIn(row["basis"], ("MapComparison", "ExactStandName"))
            referenced.extend(row["ifatcStandNames"])
        self.assertEqual(len(referenced), len(set(referenced)))
        self.assertEqual(rows["D32"]["ifatcStandNames"], ["Gate 132"])
        self.assertEqual(rows["B24"]["ifatcStandNames"], ["Gate C24"])
        self.assertEqual(rows["A11"]["ifatcStandNames"], ["Gate B11", "Gate B11A"])
        self.assertEqual(rows["D22"]["widthClass"], "D")
        self.assertEqual(rows["E71"]["widthClass"], "F")
        for terminal in airport["passengerGateGroups"]["terminals"].values():
            for group in terminal["groups"]:
                if len(group) == 2:
                    self.assertIn(group[0], rows)
                    self.assertNotIn(group[1], rows)
        self.assertEqual(rows["Remote Stand H01"]["conflictGroup"], rows["Remote Stand H01A"]["conflictGroup"])
        self.assertEqual(rows["Remote Stand H01A"]["conflictGroup"], rows["Remote Stand H01B"]["conflictGroup"])


if __name__ == "__main__":
    unittest.main()
