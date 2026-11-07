import copy
import unittest
from pathlib import Path

from smartgate_stream.validation import ACTIONS, load_scenario, read_json, validate_message, validate_scenario


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scripted.json"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(FIXTURE)

    def test_all_actions_preserved(self):
        original = copy.deepcopy(self.scenario)
        validate_scenario(self.scenario)
        self.assertEqual(self.scenario, original)
        self.assertEqual({e["message"]["action"] for e in self.scenario["events"]}, ACTIONS - {"InitializeAirport"})

    def test_initialization_preserved_and_invalid_snapshots_rejected(self):
        initial = {"action": "InitializeAirport", "flights": [copy.deepcopy(self.scenario["events"][0]["message"])],
                   "parkedAircraft": [], "gateOutages": [copy.deepcopy(self.scenario["events"][2]["message"])]}
        leg = initial["flights"][0]["legs"][0]
        initial["flights"][0]["legs"].append(dict(leg, legType="Through", departureStation=leg["arrivalStation"],
                                                arrivalStation=leg["departureStation"], scheduledDepartureTime="14:00",
                                                scheduledArrivalTime="15:15"))
        event = {"timestamp": self.scenario["events"][0]["timestamp"], "message": initial}
        self.scenario["events"].insert(0, event)
        original = copy.deepcopy(self.scenario)
        validate_scenario(self.scenario)
        self.assertEqual(self.scenario, original)
        aircraft = initial["flights"][0]["flight"]
        mutations = [
            lambda s: s["events"][1].update(message=copy.deepcopy(initial)),
            lambda s: s["events"][0]["message"].update(flights=None),
            lambda s: s["events"][0]["message"].update(flights=[initial]),
            lambda s: s["events"][0]["message"]["flights"].append(initial["flights"][0]),
            lambda s: s["events"][0]["message"]["flights"][0]["legs"][0].update(scheduledArrivalTime="25:00"),
            lambda s: s["events"][0]["message"].update(parkedAircraft=None),
            lambda s: s["events"][0]["message"].update(parkedAircraft=[{}]),
            lambda s: s["events"][0]["message"].update(parkedAircraft=[aircraft, aircraft]),
            lambda s: s["events"][0]["message"].update(parkedAircraft=[dict(aircraft, aircraftType="388")]),
            lambda s: s["events"][0]["message"].update(gateOutages=None),
            lambda s: s["events"][0]["message"].update(gateOutages=[initial["gateOutages"][0]] * 2),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                invalid = copy.deepcopy(original)
                mutate(invalid)
                with self.assertRaises(ValueError):
                    validate_scenario(invalid)

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
        outage.update(gate="Remote Stand H01A", scheduledStartTime="23:30", scheduledEndTime="00:30")
        validate_scenario(self.scenario)
        del outage["scheduledEndTime"]
        validate_scenario(self.scenario)

    def test_outages_require_exact_mapped_ifatc_stand_names(self):
        outage = self.scenario["events"][2]["message"]
        for stand in ("Gate 132", "Gate C24", "Gate B11A", "Remote Stand H01A"):
            with self.subTest(stand=stand):
                outage["gate"] = stand
                original = copy.deepcopy(outage)
                validate_message(outage)
                self.assertEqual(outage, original)
        for stand in ("D32", "F32", "B24", "Gate 999", "Gate 101", "gate 132", None, [], {}):
            with self.subTest(stand=stand):
                outage["gate"] = stand
                with self.assertRaisesRegex(ValueError, "unknown IFATC passenger stand"):
                    validate_message(outage)
        del outage["gate"]
        with self.assertRaisesRegex(ValueError, "unknown IFATC passenger stand"):
            validate_message(outage)

    def test_initial_outages_reject_duplicate_ifatc_positions_for_one_gate(self):
        outages = [{"action": "GateOutage", "gate": stand, "scheduledStartTime": "08:00"}
                   for stand in ("Gate 120", "Gate 120A")]
        initial = {"action": "InitializeAirport", "flights": [], "parkedAircraft": [], "gateOutages": outages}
        with self.assertRaisesRegex(ValueError, "duplicate initial gate outage"):
            validate_message(initial)

    def test_diversion_reasons_and_legacy_messages_are_preserved(self):
        for reason in ("turnback", "medical", "diversion", "other", None):
            with self.subTest(reason=reason):
                scenario = copy.deepcopy(self.scenario)
                message = scenario["events"][7]["message"]
                del message["emergencyState"]
                if reason is None:
                    del message["reason"]
                else:
                    message["reason"] = reason
                message["legs"][0]["departureStation"] = "YYZ" if reason == "turnback" else "YUL"
                original = copy.deepcopy(scenario)
                validate_scenario(scenario)
                self.assertEqual(scenario, original)

    def test_turnback_must_return_to_its_departure_airport(self):
        del self.scenario["events"][7]["message"]["emergencyState"]
        self.scenario["events"][7]["message"]["reason"] = "turnback"
        with self.assertRaisesRegex(ValueError, "turnback must depart from YYZ"):
            validate_scenario(self.scenario)

    def test_optional_emergency_state_on_flight_messages_is_preserved(self):
        for state in ("Turnback", "Emergency"):
            for index, event in enumerate(self.scenario["events"]):
                if event["message"]["action"] == "GateOutage":
                    continue
                with self.subTest(state=state, action=event["message"]["action"]):
                    scenario = copy.deepcopy(self.scenario)
                    message = scenario["events"][index]["message"]
                    message["emergencyState"] = state
                    if state == "Turnback" and message["action"] == "DivertFlight":
                        message["legs"][0]["departureStation"] = "YYZ"
                    original = copy.deepcopy(scenario)
                    validate_scenario(scenario)
                    self.assertEqual(scenario, original)

    def test_emergency_state_rejects_invalid_values_and_resource_messages(self):
        for state in (None, True, 1, "", "turnback", "Unknown", [], {}):
            with self.subTest(state=state):
                scenario = copy.deepcopy(self.scenario)
                scenario["events"][0]["message"]["emergencyState"] = state
                with self.assertRaisesRegex(ValueError, "invalid emergencyState"):
                    validate_scenario(scenario)
        self.scenario["events"][2]["message"]["emergencyState"] = "Emergency"
        with self.assertRaisesRegex(ValueError, "GateOutage must not contain emergencyState"):
            validate_scenario(self.scenario)

    def test_explicit_emergency_state_takes_precedence_over_reason(self):
        message = self.scenario["events"][7]["message"]
        message.update(emergencyState="Emergency", reason="turnback")
        validate_scenario(self.scenario)
        message.update(emergencyState="Turnback", reason="technical")
        with self.assertRaisesRegex(ValueError, "turnback must depart from YYZ"):
            validate_scenario(self.scenario)
        message["legs"][0]["departureStation"] = "YYZ"
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

    def test_optional_scenario_rate_and_invalid_metadata(self):
        self.scenario["metadata"] = {"rate": 0.5, "extension": "preserved"}
        validate_scenario(self.scenario)
        for metadata in (None, [], "60", {"rate": None}, {"rate": True}, {"rate": "60"},
                         {"rate": 0}, {"rate": -1}, {"rate": float("inf")}, {"rate": 10**400}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                validate_scenario({**self.scenario, "metadata": metadata})

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

    def test_traffic_types_cover_both_directions_and_reject_invalid_or_inconsistent_values(self):
        for origin, destination, category in (("YYZ", "YUL", "Domestic"), ("YUL", "YYZ", "Domestic"),
                                               ("YYZ", "JFK", "Transborder"), ("JFK", "YYZ", "Transborder"),
                                               ("YYZ", "LHR", "International"), ("CDG", "YYZ", "International"),
                                               ("YYZ", "CUN", "International")):
            with self.subTest(origin=origin, destination=destination):
                message = copy.deepcopy(self.scenario["events"][0]["message"])
                leg = message["legs"][0]
                leg.update(departureStation=origin, arrivalStation=destination, trafficType=category)
                validate_message(message)
                for invalid in (None, [], "international", "Other", "Domestic" if category != "Domestic" else "International"):
                    leg["trafficType"] = invalid
                    with self.assertRaisesRegex(ValueError, "trafficType"):
                        validate_message(message)
                del leg["trafficType"]
                with self.assertRaisesRegex(ValueError, "require trafficType"):
                    validate_message(message)

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
