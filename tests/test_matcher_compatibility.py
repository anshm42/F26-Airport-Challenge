import unittest
from datetime import datetime

import matcher_server as matcher
from matcher_server import (
    ScenarioState,
    allowed_width,
    filter_passenger_flights,
    load_references,
    parse_passenger_gate_selectors,
    passenger_gate_matches,
)


class MatcherCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aircraft_classes, cls.gates, cls.airport_timezone = load_references()

    def test_current_passenger_crosswalk_is_loaded(self):
        self.assertEqual(len(self.gates), 110)
        self.assertEqual(len({gate["conflictGroup"] for gate in self.gates}), 92)
        self.assertTrue(all(gate["ifatcStandNames"] for gate in self.gates))
        self.assertTrue(all(gate["id"] for gate in self.gates))

    def test_aircraft_capacity_matches_stream_rule(self):
        self.assertTrue(allowed_width("A", "A"))
        self.assertTrue(allowed_width("A", "F"))
        self.assertTrue(allowed_width("D", "E"))
        self.assertFalse(allowed_width("E", "D"))
        self.assertFalse(allowed_width(None, "F"))

    def test_exact_ifatc_outage_resolves_to_conflict_group(self):
        state = ScenarioState({}, self.aircraft_classes, self.gates, self.airport_timezone)
        state.epoch = datetime(2026, 11, 7)
        state.horizon_end = datetime(2026, 11, 8)
        state._add_outage(
            {"gate": "Gate 132", "scheduledStartTime": "08:00", "scheduledEndTime": "10:00"},
            datetime(2026, 11, 7),
        )
        outage = state.outages[0]
        self.assertEqual(outage["gate"], "D32")
        self.assertEqual(outage["conflictGroup"], state.gate_by_id["D32"]["conflictGroup"])
        self.assertIn(outage["conflictGroup"], state.outage_payload())

    def test_passenger_gate_selectors_match_current_and_previous_gate(self):
        selectors = parse_passenger_gate_selectors("1:D20-D30,3:A")
        self.assertTrue(passenger_gate_matches({"terminal": "1", "gate": "D24"}, selectors[0]))
        self.assertTrue(passenger_gate_matches({"terminal": "3", "gate": "A17"}, selectors[1]))
        self.assertFalse(passenger_gate_matches({"terminal": "1", "gate": "E24"}, selectors[0]))

        flights = [{
            "flightId": "AC|501||2026-11-07|LEG0",
            "gate": None,
            "previous": {"gate": {"terminal": "1", "gate": "D24"}},
        }]
        self.assertEqual(filter_passenger_flights(flights, selectors), flights)

    def test_passenger_feed_includes_current_parked_arrival_with_offset(self):
        flight = {
            "carrier": "AC",
            "registration": "C-TEST",
            "aircraftType": "789",
        }
        leg = {
            "flightNumber": "501",
            "flightSuffix": None,
            "originatingDate": "2026-11-07",
            "departureStation": "YUL",
            "arrivalStation": "YYZ",
            "scheduledDepartureTime": "06:00",
            "scheduledArrivalTime": "07:00",
            "trafficType": "Domestic",
            "codeshare": [{"carrier": "UA", "flightNumber": "8501"}],
        }
        message = {
            "action": "InitializeAirport",
            "flights": [{"action": "CreateFlight", "flight": flight, "legs": [leg]}],
            "parkedAircraft": [{**flight, "gate": "D22"}],
            "gateOutages": [],
        }
        state = ScenarioState({}, self.aircraft_classes, self.gates, self.airport_timezone)
        state.initialize({
            "timestamp": "2026-11-07T12:00:00Z",
            "message": message,
        })

        old_state = (
            matcher.STATE, matcher.ASSIGNMENTS, matcher.LATEST_SIMULATED_TIME,
            matcher.PASSENGER_LAST_FIELDS, matcher.PASSENGER_PREVIOUS_FIELDS,
        )
        try:
            matcher.STATE = state
            matcher.ASSIGNMENTS = {"PARKED:C-TEST": "D22"}
            matcher.LATEST_SIMULATED_TIME = "2026-11-07T12:00:00Z"
            matcher.PASSENGER_LAST_FIELDS = {}
            matcher.PASSENGER_PREVIOUS_FIELDS = {}
            payload = matcher.build_passenger_message()
            self.assertEqual(payload["type"], "assignments")
            self.assertEqual(len(payload["flights"]), 1)
            passenger_flight = payload["flights"][0]
            self.assertEqual(passenger_flight["flightCodes"], ["AC501", "UA8501"])
            self.assertEqual(passenger_flight["gate"], {"terminal": "1", "gate": "D22"})
            self.assertTrue(passenger_flight["timeArrival"].endswith("-05:00"))
            self.assertEqual(passenger_flight["timeDeparture"], None)
            self.assertEqual(passenger_flight["previous"], {})
        finally:
            (
                matcher.STATE, matcher.ASSIGNMENTS, matcher.LATEST_SIMULATED_TIME,
                matcher.PASSENGER_LAST_FIELDS, matcher.PASSENGER_PREVIOUS_FIELDS,
            ) = old_state


if __name__ == "__main__":
    unittest.main()
