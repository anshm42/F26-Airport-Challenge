import json
import re
import unittest
from pathlib import Path


class PearsonDataTest(unittest.TestCase):
    def test_static_snapshot_integrity(self):
        data = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "pearson.json").read_text()
        )
        aircraft_data = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "aircraft.json").read_text()
        )
        terminals = data["gateZones"]["terminals"]
        classes = aircraft_data["gateWidthClasses"]["classes"]
        stands = data["ifatcStands"]["records"]
        airlines = data["airlineTerminals"]["records"]

        self.assertNotIn("aircraftTypes", data)
        self.assertNotIn("gateWidthClasses", data)
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

        def check_sources(value, sources):
            if isinstance(value, dict):
                for source_id in value.get("sourceIds", []):
                    self.assertIn(source_id, sources)
                for child in value.values():
                    check_sources(child, sources)
            elif isinstance(value, list):
                for child in value:
                    check_sources(child, sources)

        for snapshot in (data, aircraft_data):
            check_sources(snapshot, snapshot["sources"])
        exceptions = {s["name"]: s["status"] for s in data["knownStandExceptions"]}
        self.assertEqual(exceptions["Gate 272"], "Closed")
        self.assertIn("Gate 272", {s["name"] for s in stands})

    def test_iata_aircraft_reference(self):
        data = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "aircraft.json").read_text()
        )
        aircraft = data["aircraftTypes"]
        records = aircraft["records"]
        by_code = {record["aircraftType"]: record for record in records}
        # Frozen IATA column from the linked Wikipedia snapshot, not ICAO codes.
        expected_codes = set("""
            100 141 142 143 221 223 290 295 312 313 318 319 31N 320 321 32A
            32B 32C 32D 32N 32Q 332 333 338 339 33B 33X 33Y 342 343 345 346
            351 359 388 703 717 721 722 732 733 734 735 736 738 739 73C 73E
            73F 73G 73H 73J 73K 73L 73P 73U 73W 73Y 741 742 743 744 74B 74C
            74D 74E 74H 74L 74N 74R 74T 74V 74X 74Y 752 753 75F 75W 762 763
            764 76W 76X 76Y 772 773 778 779 77L 77W 77X 781 788 789 7M7 7M8
            7M9 7MJ 919 A26 A28 A30 A32 A40 A4F A58 A5F A81 AB4 AB6 ABB ABY
            ACT AI4 AN4 AN7 ANF AR1 AR7 AR8 AT4 AT5 AT7 ATP ATR B11 B20 B72
            B99 BE4 BEH C08 C17 C27 CCJ CCX CD2 CJ1 CL3 CNT CR1 CR2 CR7 CR9
            CRK CS2 CS4 CS5 CV5 D11 D1C D1M D1X D1Y D20 D28 D38 D8L D8Q D8T
            D91 D92 D93 D94 D95 DF3 DF6 DF7 DF9 DH1 DH2 DH3 DH4 DH7 DHC DHT
            E54 E55 E70 E75 E7W E90 E95 EA5 EM2 EMB EP1 EP3 ER3 ER4 ERD F21
            F27 F50 F70 FRJ GJ4 GJ5 GJ6 GL7 GR1 GR3 GRM GRS H25 HHJ HS7 I14
            I93 IL6 IL7 IL8 ILW J31 J32 J41 K35 L10 L4T LOE LOH LRJ M11 M1F
            M1M M81 M82 M83 M87 M88 M90 MU2 ND2 NDC P18 PL2 PL4 PY2 S20 SF3
            SH3 SH6 SHB SHS SU9 SW4 T20 TU3 TU5 WWP YK2 YK4 YN2 YS1
        """.split())
        self.assertEqual(aircraft["codeSystem"], "IATA")
        self.assertEqual(set(by_code), expected_codes)
        self.assertEqual(len(records), 254)
        self.assertEqual(sum(len(record["models"]) for record in records), 277)
        self.assertEqual(len(aircraft["excludedModelsWithoutIataCode"]), 6)
        self.assertEqual(
            set(aircraft["serviceTypes"]),
            {"Passenger", "Cargo", "Combi", "SpecialPurpose"},
        )
        for record in records:
            with self.subTest(code=record["aircraftType"]):
                self.assertRegex(record["aircraftType"], r"^[A-Z0-9]{3}$")
                self.assertTrue(record["models"])
                self.assertEqual(len(record["models"]), len(set(record["models"])))
                self.assertTrue(all(model.strip() for model in record["models"]))
                self.assertTrue(record["serviceTypes"])
                self.assertEqual(len(record["serviceTypes"]), len(set(record["serviceTypes"])))
                self.assertLessEqual(set(record["serviceTypes"]), set(aircraft["serviceTypes"]))
                if record["widthClass"] is None:
                    self.assertEqual(record["widthClassBasis"], "Unclassified")
                    self.assertTrue(record["note"])
                else:
                    self.assertIn(record["widthClass"], data["gateWidthClasses"]["classes"])
                    self.assertIn(record["widthClassBasis"], {
                        "IFATCFamilyExample", "SizeEstimate", "ConservativeGroupEstimate",
                    })

        passenger_cargo_pairs = (
            ("AB6", "ABY", "D"), ("332", "33X", "E"), ("333", "33Y", "E"),
            ("732", "73F", "C"), ("733", "73Y", "C"), ("734", "73P", "C"),
            ("738", "73U", "C"), ("73H", "73K", "C"), ("741", "74T", "E"),
            ("742", "74X", "E"), ("744", "74Y", "E"), ("74H", "74N", "F"),
            ("74R", "74V", "E"), ("752", "75F", "D"), ("762", "76X", "D"),
            ("763", "76Y", "D"), ("77L", "77X", "E"), ("D11", "D1X", "D"),
            ("D1C", "D1Y", "D"), ("M11", "M1F", "D"),
        )
        for passenger, cargo, width_class in passenger_cargo_pairs:
            with self.subTest(passenger=passenger, cargo=cargo):
                self.assertEqual(by_code[passenger]["serviceTypes"], ["Passenger"])
                self.assertEqual(by_code[cargo]["serviceTypes"], ["Cargo"])
                self.assertEqual(by_code[passenger]["widthClass"], width_class)
                self.assertEqual(by_code[cargo]["widthClass"], width_class)
        for code in ("73L", "74C", "74D", "74E", "D1M", "M1M"):
            self.assertEqual(by_code[code]["serviceTypes"], ["Combi"])
        for code in ("C08", "778", "ATR"):
            self.assertEqual(by_code[code]["serviceTypes"], ["Passenger", "Cargo"])
        for code in ("A30", "K35"):
            self.assertEqual(by_code[code]["serviceTypes"], ["SpecialPurpose"])
        for code, model_count in {"ATR": 4, "CJ1": 14, "ER3": 2, "CCX": 3, "LOH": 2}.items():
            self.assertEqual(len(by_code[code]["models"]), model_count)
        self.assertEqual(by_code["CJ1"]["widthClass"], "B")
        self.assertEqual(by_code["ER3"]["widthClass"], "C")
        for code in ("CJ1", "ER3"):
            self.assertEqual(by_code[code]["widthClassBasis"], "ConservativeGroupEstimate")
        for code, width_class in {
            "EP1": "A", "C08": "B", "321": "C", "73G": "C", "789": "E",
            "388": "F", "778": "F", "779": "F",
        }.items():
            self.assertEqual(by_code[code]["widthClass"], width_class)
        self.assertEqual(
            {record["aircraftType"] for record in records if record["widthClass"] is None},
            {"A5F"},
        )


if __name__ == "__main__":
    unittest.main()
