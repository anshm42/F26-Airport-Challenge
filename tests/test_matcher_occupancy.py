"""Physical reservations and transaction checks for the native matcher."""

import copy
import unittest
from unittest.mock import patch

import matcher_server as matcher
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import initial_gate_state
from smartgate_stream.validation import flight_key, parse_utc
from test_stream_gates import INBOUND, PARKED, SPARE, assignment, flight


class MatcherOccupancyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classes, cls.all_gates, cls.timezone = matcher.load_references()

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(vars(matcher)).start()
        self.gates = [gate for gate in self.all_gates if gate["id"] in ("D20", "D22")]
        patch.object(matcher, "load_references", side_effect=lambda: (self.classes, self.gates, self.timezone)).start()

        def solve(_, payload):
            sessions = {s["id"]: s for s in payload["sessions"]}
            return matcher.repair_assignments_python(sessions, payload["gates"], payload["outages"], {}, set(sessions))

        patch.object(matcher, "run_julia", side_effect=solve).start()
        self.outbound = flight("100", "YYZ", "YUL", "06:00", "07:00", PARKED)
        self.inbound = flight("200", "YUL", "YYZ", "06:00", "07:00")
        self.returning = flight("201", "YYZ", "YUL", "09:00", "10:00")

    def initialize(self, flights=None, parked=None):
        parked, gates = initial_gate_state([{**PARKED, "gate": "D20"}] if parked is None else parked)
        initial = {"action": "InitializeAirport", "flights": [self.outbound, self.inbound, self.returning]
                   if flights is None else flights, "parkedAircraft": parked, "gateOutages": [],
                   "gateState": gates, "unassignedAircraft": []}
        self.stream = GateState()
        self.stream.apply_message(initial, "2026-11-07T05:00:00Z")
        decisions = matcher._handle_initialize(initial, "2026-11-07T05:00:00Z")
        self.stream.apply_decisions(decisions, initializing=True)
        return initial

    def update(self, message, timestamp="2026-11-07T10:30:00Z"):
        self.stream.apply_message(message, timestamp)
        decisions = matcher._handle_update(self.stream.request(message), timestamp)
        self.stream.apply_decisions(decisions)
        return decisions

    def cancel_departure(self):
        return {"action": "CancelFlight", "flight": PARKED.copy(),
                "legs": [assignment(self.outbound, None)["leg"]], "cancelScope": {"type": "SpecificLeg"}}

    def test_cancelled_departure_reserves_gate_and_reassigns_future_arrival(self):
        self.initialize()
        decisions = self.update(self.cancel_departure())
        self.assertIn(assignment(self.inbound, "D22"), decisions["assignments"])
        self.assertIn(assignment(self.returning, "D22"), decisions["assignments"])
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.stream.advance(parse_utc("2026-11-07T12:00:00Z"))
        self.assertEqual(self.stream.gates["D22"], INBOUND)
        self.assertEqual(self.stream.gates["D20"], PARKED)

    def test_without_alternate_gate_future_choice_is_cleared_and_arrival_waits(self):
        self.gates = [g for g in self.gates if g["id"] == "D20"]
        self.initialize()
        decisions = self.update(self.cancel_departure())
        self.assertIn(assignment(self.inbound, None), decisions["assignments"])
        self.assertNotIn(flight_key("AC", self.inbound["legs"][0]), self.stream.assignments)
        self.stream.advance(parse_utc("2026-11-07T12:00:00Z"))
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.assertEqual(self.stream.snapshot()["unassigned_aircraft"], [INBOUND])

    def test_missing_current_assignment_does_not_release_occupied_gate(self):
        self.initialize([self.outbound])
        matcher.ASSIGNMENTS.clear()
        decisions = self.update(self.inbound)
        self.assertIn(assignment(self.outbound, "D20"), decisions["assignments"])
        self.assertEqual(matcher.ASSIGNMENTS["PARKED:" + PARKED["registration"]], "D20")
        self.assertEqual(self.stream.gates["D20"], PARKED)

    def test_new_arrival_without_internal_assignment_keeps_its_physical_gate(self):
        self.initialize([self.inbound, self.returning], [])
        self.stream.advance(parse_utc("2026-11-07T12:30:00Z"))
        matcher.ASSIGNMENTS.clear()
        later = flight("300", "YUL", "YYZ", "07:00", "08:00", SPARE)
        decisions = self.update(later, "2026-11-07T12:30:00Z")
        self.assertIn(assignment(later, "D22"), decisions["assignments"])
        self.assertEqual(self.stream.gates["D20"], INBOUND)
        self.assertFalse(decisions["moves"])

    def test_idle_aircraft_missing_from_schedule_still_has_a_reservation(self):
        initial = self.initialize([], [])
        initial["gateState"]["D20"] = SPARE.copy()
        sessions = matcher.STATE.build_sessions(gate_state=initial["gateState"], simulated_time="2026-11-07T06:00:00Z")
        self.assertEqual(len(sessions), 1)
        session = next(iter(sessions.values()))
        self.assertEqual((session["registration"], session["occupiedGate"], session["start"], session["end"]),
                         (SPARE["registration"], "D20", 60, 1440))

    def test_equipment_swap_keeps_both_physically_parked_aircraft(self):
        self.initialize([self.outbound], [{**PARKED, "gate": "D20"}, {**SPARE, "gate": "D22"}])
        self.update({"action": "UpdateEquipment", "flight": SPARE.copy(),
                     "legs": [assignment(self.outbound, None)["leg"]], "equipmentUpdate": {"scope": "AllLegs"}})
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.assertEqual(self.stream.gates["D22"], SPARE)
        self.assertEqual(set(matcher.STATE.parked), {PARKED["registration"], SPARE["registration"]})
        self.assertEqual(set(matcher.ASSIGNMENTS.values()), {"D20", "D22"})

    def accepted_state(self):
        return copy.deepcopy({name: getattr(matcher, name) for name in (
            "ASSIGNMENTS", "GATE_STARTS", "LATEST_GATE_STATE", "LATEST_UNASSIGNED_AIRCRAFT",
            "LATEST_SIMULATED_TIME", "PREDICTED_GATE_STATE", "PREDICTED_UNASSIGNED_AIRCRAFT",
            "LAST_SOLVER_SUMMARY", "LAST_DECISION_RESPONSE", "PASSENGER_LAST_FIELDS",
            "PASSENGER_PREVIOUS_FIELDS", "LAST_PASSENGER_FLIGHTS_JSON",
        )})

    def test_failed_outage_relocation_preserves_all_accepted_state(self):
        self.gates = [g for g in self.gates if g["id"] == "D20"]
        self.initialize([self.outbound])
        state, before = matcher.STATE, self.accepted_state()
        outage = {"action": "GateOutage", "gate": self.gates[0]["ifatcStandNames"][0],
                  "scheduledStartTime": "05:30", "scheduledEndTime": "08:00"}
        self.stream.apply_message(outage, "2026-11-07T10:30:00Z")
        with self.assertRaisesRegex(RuntimeError, "No immediate compatible gate"):
            matcher._handle_update(self.stream.request(outage), "2026-11-07T10:30:00Z")
        self.assertIs(matcher.STATE, state)
        self.assertEqual(self.accepted_state(), before)
        self.assertEqual(self.stream.gates["D20"], PARKED)

    def test_invalid_initial_plan_does_not_reset_accepted_state_or_passenger_cache(self):
        initial = self.initialize()
        matcher.PASSENGER_LAST_FIELDS = {"existing": {"gate": "D20"}}
        state, before = matcher.STATE, self.accepted_state()
        with patch.object(matcher, "run_julia", return_value={"assignments": [], "unassigned": []}):
            with self.assertRaisesRegex(RuntimeError, "Missing immediate reservation"):
                matcher._handle_initialize(initial, "2026-11-07T05:00:00Z")
        self.assertIs(matcher.STATE, state)
        self.assertEqual(self.accepted_state(), before)

    def test_late_validation_failure_rolls_back_entire_update(self):
        self.initialize()
        state, before = matcher.STATE, self.accepted_state()
        with patch.object(matcher, "predict_post_decision_gate_state", side_effect=RuntimeError("invalid move batch")):
            with self.assertRaisesRegex(RuntimeError, "invalid move batch"):
                self.update(self.cancel_departure())
        self.assertIs(matcher.STATE, state)
        self.assertEqual(self.accepted_state(), before)

    def test_outage_moves_current_aircraft_to_a_free_compatible_gate(self):
        self.initialize([self.outbound])
        gate = next(g for g in self.gates if g["id"] == "D20")
        decisions = self.update({"action": "GateOutage", "gate": gate["ifatcStandNames"][0],
                                 "scheduledStartTime": "05:30", "scheduledEndTime": "08:00"})
        self.assertEqual(decisions["moves"], [{"registration": PARKED["registration"], "gate": "D22"}])
        self.assertIsNone(self.stream.gates["D20"])
        self.assertEqual(self.stream.gates["D22"], PARKED)

    def test_physical_reservation_precedes_future_booking_in_shared_group(self):
        gates = [{"id": "D20", "conflictGroup": "shared", "widthClass": "C"},
                 {"id": "D21", "conflictGroup": "shared", "widthClass": "C"},
                 {"id": "D22", "conflictGroup": "other", "widthClass": "C"}]
        sessions = {"current": {"id": "current", **PARKED, "widthClass": "C", "start": 0, "end": 100, "occupiedGate": "D20"},
                    "future": {"id": "future", **INBOUND, "widthClass": "C", "start": 10, "end": 20}}
        result = matcher.repair_assignments_python(sessions, gates, {}, {"future": "D21"}, set())
        self.assertEqual(dict(result["assignments"]), {"current": "D20"})
        self.assertEqual(dict(result["reassignments"]), {"future": "D22"})

    def test_relocation_repairs_future_booking_at_destination(self):
        inbound = flight("210", "YUL", "YYZ", "04:45", "05:45")
        outbound = flight("211", "YYZ", "YUL", "05:55", "06:55")
        self.initialize([self.outbound, inbound, outbound])
        gate = next(g for g in self.gates if g["id"] == "D20")
        decisions = self.update({"action": "GateOutage", "gate": gate["ifatcStandNames"][0],
                                 "scheduledStartTime": "05:30", "scheduledEndTime": "08:00"})
        self.assertEqual(self.stream.gates["D22"], PARKED)
        self.assertIn(assignment(inbound, None), decisions["assignments"])
        self.assertIn(assignment(outbound, None), decisions["assignments"])

    def test_complete_plan_rejects_overlap_in_shared_group(self):
        gates = [{"id": "D20", "conflictGroup": "shared", "widthClass": "C"},
                 {"id": "D21", "conflictGroup": "shared", "widthClass": "C"}]
        sessions = {"one": {**PARKED, "start": 0, "end": 100, "widthClass": "C"},
                    "two": {**INBOUND, "start": 10, "end": 20, "widthClass": "C"}}
        with self.assertRaisesRegex(RuntimeError, "Overlapping passenger gate reservations"):
            matcher.validate_assignments(sessions, gates, {}, {"one": "D20", "two": "D21"}, {})

    def test_complete_plan_rejects_waiting_for_an_already_occupied_aircraft(self):
        sessions = {"current": {**PARKED, "start": 0, "end": 100, "widthClass": "C", "occupiedGate": "D20"}}
        with self.assertRaisesRegex(RuntimeError, "Missing immediate reservation"):
            matcher.validate_assignments(sessions, self.gates, {}, {"current": "D20"}, {"current": 10})

    def test_valid_atomic_swap_preserves_both_aircraft(self):
        gates = [{"id": "D20", "conflictGroup": "D20", "widthClass": "C", "allowedTrafficTypes": ["Domestic"]},
                 {"id": "D22", "conflictGroup": "D22", "widthClass": "C", "allowedTrafficTypes": ["International"]}]
        self.initialize([], [{**PARKED, "gate": "D20"}, {**SPARE, "gate": "D22"}])
        sessions = {
            "one": {"id": "one", **PARKED, "widthClass": "C", "start": 0, "end": 100,
                    "occupiedGate": "D20", "departureTrafficType": "International"},
            "two": {"id": "two", **SPARE, "widthClass": "C", "start": 0, "end": 100,
                    "occupiedGate": "D22", "departureTrafficType": "Domestic"},
        }
        result = matcher.repair_assignments_python(sessions, gates, {}, {"one": "D20", "two": "D22"}, set(sessions))
        assignments = dict(result["reassignments"])
        matcher.validate_assignments(sessions, gates, {}, assignments, result["gate_starts"])
        snapshot = self.stream.request({})
        decisions = matcher.build_stream_decisions(matcher.STATE, sessions, assignments, result["gate_starts"],
                                                   set(sessions), snapshot=snapshot, simulated_time="2026-11-07T05:00:00Z")
        predicted, _ = matcher.predict_post_decision_gate_state(
            matcher.STATE, sessions, assignments, result["gate_starts"], set(sessions),
            snapshot=snapshot, simulated_time="2026-11-07T05:00:00Z")
        self.stream.apply_decisions(decisions)
        self.assertEqual(self.stream.gates, predicted)
        self.assertEqual(self.stream.gates["D20"], SPARE)
        self.assertEqual(self.stream.gates["D22"], PARKED)


if __name__ == "__main__":
    unittest.main()
