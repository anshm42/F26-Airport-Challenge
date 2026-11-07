import json
import re
import unittest
from pathlib import Path


class PearsonDataTest(unittest.TestCase):
    def test_static_snapshot_integrity(self):
        data = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "pearson.json").read_text()
        )
        terminals = data["gateZones"]["terminals"]
        classes = data["gateWidthClasses"]["classes"]
        stands = data["ifatcStands"]["records"]
        airlines = data["airlineTerminals"]["records"]

        self.assertEqual(set(terminals), {"T1", "T3"})
        self.assertEqual(set(classes), set("ABCDEF"))
        self.assertEqual(len(stands), 286)
        self.assertEqual(sum(s["name"].startswith("Gate ") for s in stands), 178)
        self.assertEqual(len({s["name"] for s in stands}), len(stands))
        self.assertTrue(all(s["widthClass"] in classes for s in stands))
        self.assertEqual(len(airlines), 54)
        self.assertEqual(len({a["carrier"] for a in airlines}), len(airlines))
        for airline in airlines:
            self.assertRegex(airline["carrier"], r"^[A-Z0-9]{2}$")
            self.assertRegex(airline["icao"], r"^[A-Z]{3}$")
            self.assertIn(airline["terminal"], terminals)
        defaults = {a["carrier"]: a["terminal"] for a in airlines}
        for carrier in ("AC", "UA", "EK", "EY", "AZ"):
            self.assertEqual(defaults[carrier], "T1")
        for carrier in ("WS", "PD", "TS", "AA", "DL", "SK"):
            self.assertEqual(defaults[carrier], "T3")

        swings = set()
        group_count = 0
        for terminal, inventory in data["passengerGateGroups"]["terminals"].items():
            aliases = [gate for group in inventory["groups"] for gate in group]
            self.assertEqual(len(aliases), len(set(aliases)))
            for group in inventory["groups"]:
                self.assertIn(len(group), (1, 2))
                for gate in group:
                    self.assertRegex(gate, r"^[A-F][0-9]+[a-z]?$")
                    self.assertIn(gate[0], terminals[terminal])
                if len(group) == 2:
                    self.assertEqual(re.search(r"\d+", group[0])[0], re.search(r"\d+", group[1])[0])
                    swings.add(tuple(group))
            group_count += len(inventory["groups"])
        self.assertEqual(group_count, 89)
        expected_swings = {(f"D{n}", f"F{n}") for n in (32, 34, 36, 51, 53, 55, 57)}
        expected_swings |= {(f"E{n}", f"F{n}") for n in (66, 67, 68, 69)}
        expected_swings |= {(f"A{n}", f"B{n}") for n in (17, 18, 19, 20)}
        self.assertEqual(swings, expected_swings)

        def check_sources(value):
            if isinstance(value, dict):
                for source_id in value.get("sourceIds", []):
                    self.assertIn(source_id, data["sources"])
                for child in value.values():
                    check_sources(child)
            elif isinstance(value, list):
                for child in value:
                    check_sources(child)

        check_sources(data)
        exceptions = {s["name"]: s["status"] for s in data["knownStandExceptions"]}
        self.assertEqual(exceptions["Gate 272"], "Closed")
        self.assertIn("Gate 272", {s["name"] for s in stands})


if __name__ == "__main__":
    unittest.main()
