import copy
import unittest
from pathlib import Path

from smartgate_stream.validation import ACTIONS, load_scenario, read_json, validate_scenario


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scripted.json"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(FIXTURE)

    def test_all_actions_preserved(self):
        original = copy.deepcopy(self.scenario)
        validate_scenario(self.scenario)
        self.assertEqual(self.scenario, original)
        self.assertEqual({e["message"]["action"] for e in self.scenario["events"]}, ACTIONS)

    def test_connected_route_codeshares_suffix_and_overnight(self):
        message = self.scenario["events"][0]["message"]
        leg = message["legs"][0]
        leg.update(flightSuffix=None, scheduledDepartureTime="23:30", scheduledArrivalTime="00:45")
        second = dict(leg, legType="Through", flightNumber="457",
                      originatingDate="2026-11-08", departureStation="YUL", arrivalStation="YYZ")
        message["legs"].append(second)
        validate_scenario(self.scenario)

    def test_utc_offsets_equal_times_and_indefinite_outage(self):
        self.scenario["events"][0]["timestamp"] = "2026-11-07T12:30:00.000001+00:00"
        outage = self.scenario["events"][2]["message"]
        outage.update(gate="B2a", scheduledStartTime="23:30", scheduledEndTime="00:30")
        validate_scenario(self.scenario)
        del outage["scheduledEndTime"]
        validate_scenario(self.scenario)

    def test_invalid_boundaries(self):
        mutations = [
            lambda s: s.update(events=[]),
            lambda s: s.update(events={}),
            lambda s: s["events"].__setitem__(0, None),
            lambda s: s["events"][0].update(timestamp="2026-11-07T12:30:00"),
            lambda s: s["events"][0].update(timestamp="2026-11-07T12:30:00-05:00"),
            lambda s: s["events"][0].update(timestamp="2026-02-30T12:30:00Z"),
            lambda s: s["events"][1].update(timestamp="2026-11-07T12:29:00Z"),
            lambda s: s["events"][0].update(message=[]),
            lambda s: s["events"][0]["message"].update(action=[]),
            lambda s: s["events"][0]["message"].update(action="Unknown"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                scenario = copy.deepcopy(self.scenario)
                mutate(scenario)
                with self.assertRaises(ValueError):
                    validate_scenario(scenario)

    def test_invalid_flight_fields(self):
        for key, value in (("carrier", "ac"), ("carrier", 12), ("aircraftType", "A321"),
                           ("registration", ""), ("registration", "c-fceu"), ("registration", "C FCEU")):
            with self.subTest(key=key, value=value):
                scenario = copy.deepcopy(self.scenario)
                scenario["events"][0]["message"]["flight"][key] = value
                with self.assertRaises(ValueError):
                    validate_scenario(scenario)

    def test_invalid_leg_fields(self):
        for key, value in (("flightNumber", 123), ("flightNumber", "12345"),
                           ("flightNumber", "１２３"), ("flightSuffix", "AB"),
                           ("originatingDate", "2026-02-29"), ("originatingDate", "20261107"),
                           ("departureStation", "yyz"), ("arrivalStation", "Y2L"),
                           ("scheduledDepartureTime", "24:00"), ("scheduledArrivalTime", "8:00"),
                           ("legType", "Through"), ("codeshare", None),
                           ("codeshare", [{"carrier": "UA", "flightNumber": "12345"}])):
            with self.subTest(key=key, value=value):
                scenario = copy.deepcopy(self.scenario)
                scenario["events"][0]["message"]["legs"][0][key] = value
                with self.assertRaises(ValueError):
                    validate_scenario(scenario)

    def test_invalid_action_requirements(self):
        cases = [(0, "flight", None), (0, "legs", []), (0, "legs", [None]),
                 (2, "flight", {}), (2, "legs", []), (2, "scheduledEndTime", None),
                 (2, "scheduledStartTime", "12:60"), (4, "equipmentUpdate", {}),
                 (5, "cancelScope", {"type": "Other"}), (7, "reason", 123)]
        for index, key, value in cases:
            with self.subTest(index=index, key=key):
                scenario = copy.deepcopy(self.scenario)
                scenario["events"][index]["message"][key] = value
                with self.assertRaises(ValueError):
                    validate_scenario(scenario)
        for index in (4, 5, 6, 7):
            scenario = copy.deepcopy(self.scenario)
            legs = scenario["events"][index]["message"]["legs"]
            legs.append(copy.deepcopy(legs[0]))
            with self.assertRaises(ValueError):
                validate_scenario(scenario)
        scenario = copy.deepcopy(self.scenario)
        scenario["events"][7]["message"]["legs"][0]["arrivalStation"] = "YUL"
        with self.assertRaises(ValueError):
            validate_scenario(scenario)

    def test_disconnected_route_and_non_json_extensions(self):
        message = self.scenario["events"][0]["message"]
        message["legs"].append(dict(message["legs"][0], legType="Through"))
        with self.assertRaises(ValueError):
            validate_scenario(self.scenario)
        message["legs"].pop()
        message["extension"] = float("nan")
        with self.assertRaises(ValueError):
            validate_scenario(self.scenario)
        with self.assertRaises(ValueError):
            read_json('{"events": [], "extension": NaN}')


if __name__ == "__main__":
    unittest.main()
