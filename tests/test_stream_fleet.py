import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartgate_stream.fleet import ICAO_TO_IATA, import_fleet
from smartgate_stream.validation import load_scenario


ROOT = Path(__file__).resolve().parents[1]


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        profiles = json.loads((ROOT / "data" / "stream_profiles.json").read_text())
        profiles["carriers"] = [p for p in profiles["carriers"] if p["carrier"] in ("AC", "EK")]
        (self.directory / "stream_profiles.json").write_text(json.dumps(profiles))
        self.path = self.directory / "input.csv"
        self.rows = [dict(icao24="c01764", registration="C-FITL", typecode="B77W", operatorIcao="ACA",
                          operatorIata="", status="", regUntil=""),
                     dict(icao24="8963a0", registration="A6-EOA", typecode="A388", operatorIcao="UAE",
                          operatorIata="EK", status="active", regUntil="")]

    def extract(self, rows):
        with self.path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(self.rows[0]), quotechar="'", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        with patch("smartgate_stream.fleet.DATA", self.directory):
            return import_fleet(self.path)

    def test_single_quoted_csv_blank_iata_icao_mapping_and_checksum(self):
        data = self.extract(self.rows)
        self.assertEqual([r["aircraftType"] for r in data["records"]], ["77W", "388"])
        self.assertEqual(data["sourceRows"], 2)
        self.assertEqual(data["sourceSha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(data["snapshotMonth"], "2025-08")

    def test_rejects_bad_owner_country_type_status_and_expired_registration(self):
        invalid = []
        for fields in ({"registration": "9M-ABC"}, {"registration": "C-F123"},
                       {"operatorIcao": "MAS"}, {"operatorIata": "MH"},
                       {"typecode": "GLID"}, {"icao24": "zzzzzz"},
                       {"status": "destroyed"}, {"regUntil": "2024-01-01"},
                       {"regUntil": "not a date"}):
            invalid.append({**self.rows[0], **fields})
        data = self.extract(self.rows + invalid)
        self.assertEqual(len(data["records"]), 2)

    def test_exact_duplicates_deduplicate_and_conflicting_tail_is_excluded(self):
        conflict = dict(self.rows[0], typecode="B789")
        substitute = dict(self.rows[0], registration="C-FITU", icao24="c01765")
        data = self.extract(self.rows + [copy.deepcopy(self.rows[0]), conflict, substitute])
        self.assertEqual(data["excludedConflictingRegistrations"], ["C-FITL"])
        self.assertEqual({r["registration"] for r in data["records"]}, {"C-FITU", "A6-EOA"})

    def test_invalid_schema_missing_carrier_and_malformed_row(self):
        with self.assertRaisesRegex(ValueError, "no eligible aircraft"):
            self.extract(self.rows[:1])
        self.path.write_text('"registration","typecode"\n"C-FITL","B77W"\n')
        with patch("smartgate_stream.fleet.DATA", self.directory), self.assertRaises(ValueError):
            import_fleet(self.path)
        self.extract(self.rows)
        with self.path.open("a") as output:
            output.write("'too','few'\n")
        with patch("smartgate_stream.fleet.DATA", self.directory), self.assertRaisesRegex(ValueError, "malformed CSV row"):
            import_fleet(self.path)

    def test_checked_in_pool_has_only_explicit_pearson_operators_and_known_types(self):
        data = json.loads((ROOT / "data" / "opensky_fleet_2025.json").read_text())
        profiles = {p["carrier"]: p for p in json.loads((ROOT / "data" / "stream_profiles.json").read_text())["carriers"]}
        pearson = json.loads((ROOT / "data" / "pearson.json").read_text())
        carriers = {p["carrier"] for p in pearson["airlineTerminals"]["records"]}
        self.assertEqual(data["snapshotMonth"], "2025-08")
        self.assertEqual(len(data["sourceSha256"]), 64)
        self.assertGreater(len(data["records"]), 1000)
        self.assertEqual(len(data["records"]), len({r["registration"] for r in data["records"]}))
        for record in data["records"]:
            profile = profiles[record["carrier"]]
            self.assertIn(record["carrier"], carriers)
            self.assertIn(record["operatorIcao"], profile["operatorIcaos"])
            self.assertIn(record["operatorIata"], ["", *profile["operatorIatas"]])
            self.assertRegex(record["registration"], "^" + profile["registrationPattern"] + "$")
            self.assertEqual(record["aircraftType"], ICAO_TO_IATA[record["icaoType"]])
        records = {r["registration"]: r for r in data["records"]}
        self.assertEqual((records["C-FITL"]["carrier"], records["C-FITL"]["aircraftType"]), ("AC", "77W"))
        self.assertEqual((records["C-FFEL"]["carrier"], records["C-FFEL"]["aircraftType"]), ("F8", "7M8"))

    def test_scripted_fixture_uses_snapshot_registration_type_pairs(self):
        data = json.loads((ROOT / "data" / "opensky_fleet_2025.json").read_text())
        known = {(r["carrier"], r["registration"], r["aircraftType"]) for r in data["records"]}
        for event in load_scenario(ROOT / "fixtures" / "scripted.json")["events"]:
            flight = event["message"].get("flight")
            if flight:
                self.assertIn((flight["carrier"], flight["registration"], flight["aircraftType"]), known)


if __name__ == "__main__":
    unittest.main()
