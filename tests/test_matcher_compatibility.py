import unittest
from datetime import datetime
from unittest.mock import patch

import matcher_server as matcher
from matcher_server import (
    ScenarioState,
    allowed_width,
    filter_passenger_flights,
    load_references,
    parse_passenger_gate_selectors,
    passenger_gate_matches,
)
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import initial_gate_state
from test_stream_gates import INBOUND, assignment, flight


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

    def test_cancelled_departure_updates_every_arrival_in_the_merged_visit(self):
        inbound = flight("200", "YUL", "YYZ", "05:00", "06:00")
        outbound = flight("201", "YYZ", "YUL", "07:00", "08:00")
        returning = flight("202", "YUL", "YYZ", "09:00", "10:00")
        later_departure = flight("203", "YYZ", "YUL", "11:00", "12:00")
        _, gates = initial_gate_state([])
        initial = {"action": "InitializeAirport", "flights": [inbound, outbound, returning, later_departure],
                   "parkedAircraft": [], "gateOutages": [], "gateState": gates}
        stream = GateState()
        stream.apply_message(initial, "2026-11-07T05:00:00Z")
        stream.apply_decisions({"assignments": [assignment(inbound, "D31"), assignment(outbound, "D31"),
                                               assignment(returning, "D22"), assignment(later_departure, "D22")]})
        state = ScenarioState({}, self.aircraft_classes, self.gates, self.airport_timezone)
        state.initialize({"timestamp": "2026-11-07T05:00:00Z", "message": initial})
        cancel = {"action": "CancelFlight", "flight": INBOUND.copy(), "legs": [assignment(outbound, None)["leg"]],
                  "cancelScope": {"type": "SpecificLeg"}}
        timestamp = "2026-11-07T11:30:00Z"
        stream.apply_message(cancel, timestamp)
        state.apply_message({"timestamp": timestamp, "message": cancel})
        sessions = state.build_sessions()
        self.assertEqual(len(sessions), 1)
        with patch.object(matcher, "LATEST_GATE_STATE", stream.gates), \
             patch.object(matcher, "LATEST_UNASSIGNED_AIRCRAFT", []), \
             patch.object(matcher, "LATEST_SIMULATED_TIME", timestamp):
            decisions = matcher.build_stream_decisions(state, sessions, {sid: "D38" for sid in sessions}, {}, include_all=True)
        self.assertIn(assignment(returning, "D38"), decisions["assignments"])
        stream.apply_decisions(decisions)
        self.assertEqual(stream.gates["D38"], INBOUND)

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
