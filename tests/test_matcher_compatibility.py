import unittest
from datetime import datetime

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


if __name__ == "__main__":
    unittest.main()
