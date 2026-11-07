import copy
import unittest

from smartgate_stream.gates import GateState
from smartgate_stream.inventory import canonical_gate, initial_gate_state, passenger_aircraft
from smartgate_stream.validation import parse_utc, validate_message


PARKED = {"carrier": "AC", "registration": "C-FFGZ", "aircraftType": "321"}
INBOUND = {"carrier": "AC", "registration": "C-FFIQ", "aircraftType": "321"}
SPARE = {"carrier": "AC", "registration": "C-FABC", "aircraftType": "321"}


def flight(number, origin, destination, departure, arrival, aircraft=INBOUND):
    return {"action": "CreateFlight", "flight": aircraft.copy(), "legs": [{
        "legType": "Originating", "flightNumber": number, "flightSuffix": None,
        "originatingDate": "2026-11-07", "departureStation": origin, "arrivalStation": destination,
        "scheduledDepartureTime": departure, "scheduledArrivalTime": arrival}]}


def assignment(message, gate):
    leg = message["legs"][0]
    return {"carrier": message["flight"]["carrier"], "leg": {field: leg.get(field) for field in (
        "flightNumber", "flightSuffix", "originatingDate", "departureStation", "arrivalStation")}, "gate": gate}


class GateStateTests(unittest.TestCase):
    def setUp(self):
        self.departure = flight("100", "YYZ", "YUL", "06:00", "07:15", PARKED)
        self.arrival = flight("200", "YUL", "YYZ", "07:00", "08:00")
        self.return_flight = flight("201", "YYZ", "YUL", "09:00", "10:15")

    def initialized(self, flights=None, parked=None):
        flights = [self.departure, self.arrival, self.return_flight] if flights is None else flights
        parked = [{**PARKED, "gate": "D20"}] if parked is None else parked
        parked, gates = initial_gate_state(parked)
        message = {"action": "InitializeAirport", "flights": flights, "parkedAircraft": parked,
                   "gateOutages": [], "gateState": gates}
        state = GateState()
        state.apply_message(message, "2026-11-07T05:00:00Z")
        return state

    def test_only_grounded_aircraft_occupy_initial_positions(self):
        state = self.initialized()
        self.assertEqual(len(state.gates), 121)
        self.assertEqual([plane for plane in state.gates.values() if plane], [PARKED])
        self.assertEqual(canonical_gate("F32"), "D32")
        self.assertNotIn("F32", state.gates)
        self.assertNotIn("Gate 101", state.gates)
        request = state.request(self.arrival)
        self.assertEqual(request["gateState"], state.gates)
        self.assertEqual(request["unassignedAircraft"], [])
        request["gateState"]["D20"] = None
        self.assertEqual(state.gates["D20"], PARKED)

    def test_future_choices_apply_at_arrival_and_departure_frees_gate(self):
        state = self.initialized()
        state.apply_decisions({"assignments": [assignment(self.arrival, "D20"),
                                               assignment(self.return_flight, "D20")]}, initializing=True)
        self.assertEqual(state.gates["D20"], PARKED)
        state.advance(parse_utc("2026-11-07T12:59:00Z"))
        self.assertIsNone(state.gates["D20"])
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        self.assertEqual(state.gates["D20"], INBOUND)
        state.advance(parse_utc("2026-11-07T14:00:00Z"))
        self.assertIsNone(state.gates["D20"])

    def test_later_visit_does_not_move_aircraft_currently_parked(self):
        arrival = flight("101", "YUL", "YYZ", "11:00", "12:00", PARKED)
        departure = flight("102", "YYZ", "YUL", "14:00", "15:15", PARKED)
        state = self.initialized([self.departure, arrival, departure])
        state.apply_decisions({"assignments": [assignment(arrival, "D31"), assignment(departure, "D31")]})
        self.assertEqual(state.gates["D20"], PARKED)
        self.assertIsNone(state.gates["D31"])
        state.advance(parse_utc("2026-11-07T17:00:00Z"))
        self.assertEqual(state.gates["D31"], PARKED)
        self.assertIsNone(state.gates["D20"])

    def test_cancellation_does_not_remove_a_parked_aircraft(self):
        state = self.initialized([self.departure])
        cancel = {"action": "CancelFlight", "flight": PARKED.copy(),
                  "legs": [assignment(self.departure, None)["leg"]], "cancelScope": {"type": "SpecificLeg"}}
        state.apply_message(cancel, "2026-11-07T10:30:00Z")
        state.advance(parse_utc("2026-11-07T12:00:00Z"))
        self.assertEqual(state.gates["D20"], PARKED)
        cancel["action"] = "ReinstateFlight"
        del cancel["cancelScope"]
        state.apply_message(cancel, "2026-11-07T12:05:00Z")
        self.assertIsNone(state.gates["D20"])

    def test_early_arrival_revises_when_occupancy_changes(self):
        state = self.initialized()
        state.apply_decisions({"assignments": [assignment(self.arrival, "D22")]})
        update = copy.deepcopy(self.arrival)
        update["action"] = "UpdateTiming"
        update["legs"][0]["scheduledArrivalTime"] = "07:40"
        state.apply_message(update, "2026-11-07T12:00:00Z")
        state.advance(parse_utc("2026-11-07T12:39:00Z"))
        self.assertIsNone(state.gates["D22"])
        state.advance(parse_utc("2026-11-07T12:40:00Z"))
        self.assertEqual(state.gates["D22"], INBOUND)

    def test_invalid_responses_do_not_partially_change_state(self):
        state = self.initialized([], [{**PARKED, "gate": "D20"}, {**INBOUND, "gate": "D22"}])
        for response in (
            {"moves": [{"registration": PARKED["registration"], "gate": "D31"},
                       {"registration": INBOUND["registration"], "gate": "Unknown"}]},
            {"moves": [{"registration": PARKED["registration"], "gate": "D22"}]},
            {"moves": [{"registration": SPARE["registration"], "gate": "D31"}]},
            {"assignments": [assignment(self.arrival, "D31")]},
            {"gate": "D31"},
        ):
            with self.subTest(response=response):
                before = state.snapshot()
                with self.assertRaises(ValueError):
                    state.apply_decisions(response)
                self.assertEqual(state.snapshot(), before)

    def test_swaps_aliases_and_overlapping_h_positions(self):
        state = self.initialized([], [{**PARKED, "gate": "D20"}, {**INBOUND, "gate": "D22"}])
        state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "D22"},
                                         {"registration": INBOUND["registration"], "gate": "D20"}]})
        self.assertEqual(state.gates["D22"], PARKED)
        state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "F32"}]})
        self.assertEqual(state.gates["D32"], PARKED)
        before = state.snapshot()
        with self.assertRaises(ValueError):
            state.apply_decisions({"moves": [
                {"registration": PARKED["registration"], "gate": "Remote Stand H01A"},
                {"registration": INBOUND["registration"], "gate": "Remote Stand H01B"}]})
        self.assertEqual(state.snapshot(), before)
        state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "Remote Stand H01A"}]})
        self.assertEqual(state.gates["Remote Stand H01A"], PARKED)
        with self.assertRaises(ValueError):
            state.apply_decisions({"moves": [{"registration": INBOUND["registration"], "gate": "Remote Stand H01"}]})

    def test_arrival_waits_instead_of_overwriting_an_occupant(self):
        state = self.initialized([self.arrival])
        state.apply_decisions({"assignments": [assignment(self.arrival, "D20")]})
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        self.assertEqual(state.gates["D20"], PARKED)
        self.assertEqual(state.snapshot()["unassigned_aircraft"], [INBOUND])
        state.apply_decisions({"assignments": [assignment(self.arrival, "D31")]})
        self.assertEqual(state.gates["D31"], INBOUND)
        self.assertEqual(state.snapshot()["unassigned_aircraft"], [])

    def test_outage_blocks_moves_and_future_occupancy(self):
        state = self.initialized([self.arrival])
        state.apply_message({"action": "GateOutage", "gate": "D22", "scheduledStartTime": "00:00",
                             "scheduledEndTime": "10:00"}, "2026-11-07T05:01:00Z")
        state.apply_decisions({"assignments": [assignment(self.arrival, "D22")]})
        with self.assertRaises(ValueError):
            state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "D22"}]})
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        self.assertIsNone(state.gates["D22"])
        self.assertEqual(state.snapshot()["unassigned_aircraft"], [INBOUND])

    def test_initial_positions_are_not_replaced_implicitly(self):
        state = self.initialized([self.departure])
        with self.assertRaises(ValueError):
            state.apply_decisions({"assignments": [assignment(self.departure, "D22")]}, initializing=True)
        state.apply_decisions({"assignments": [assignment(self.departure, "D22")],
                               "moves": [{"registration": PARKED["registration"], "gate": "D22"}]}, initializing=True)
        self.assertEqual(state.gates["D22"], PARKED)

    def test_equipment_swap_does_not_erase_the_original_aircraft(self):
        state = self.initialized([self.arrival, self.return_flight], [{**SPARE, "gate": "D22"}])
        state.apply_decisions({"assignments": [assignment(self.arrival, "D31"),
                                               assignment(self.return_flight, "D31")]})
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        state.apply_message({"action": "UpdateEquipment", "flight": SPARE.copy(),
                             "legs": [assignment(self.return_flight, None)["leg"]],
                             "equipmentUpdate": {"scope": "AllLegs"}}, "2026-11-07T13:10:00Z")
        state.apply_decisions({"assignments": [assignment(self.return_flight, "D22")]})
        state.advance(parse_utc("2026-11-07T14:00:00Z"))
        self.assertIsNone(state.gates["D22"])
        self.assertEqual(state.gates["D31"], INBOUND)

    def test_replacement_keeps_unchanged_leg_plan_and_completed_movements(self):
        state = self.initialized()
        state.apply_decisions({"assignments": [assignment(self.arrival, "D31")]})
        replacement = copy.deepcopy(self.arrival)
        replacement["action"] = "ReplaceFlight"
        replacement["legs"][0]["scheduledArrivalTime"] = "08:10"
        state.apply_message(replacement, "2026-11-07T12:00:00Z")
        state.advance(parse_utc("2026-11-07T13:10:00Z"))
        self.assertEqual(state.gates["D31"], INBOUND)
        replacement["legs"][0]["scheduledArrivalTime"] = "08:20"
        state.apply_message(replacement, "2026-11-07T13:15:00Z")
        state.advance(parse_utc("2026-11-07T13:20:00Z"))
        self.assertEqual(state.gates["D31"], INBOUND)

    def test_nonpassenger_types_duplicate_positions_and_future_initial_gates_are_rejected(self):
        for code in ("73Y", "73L", "K35", "C08", "H25"):
            self.assertFalse(passenger_aircraft({**PARKED, "aircraftType": code}))
            with self.assertRaises(ValueError):
                initial_gate_state([{**PARKED, "aircraftType": code, "gate": "D20"}])
        with self.assertRaises(ValueError):
            initial_gate_state([{**PARKED, "gate": "D32"}, {**INBOUND, "gate": "F32"}])
        future = copy.deepcopy(self.arrival)
        future["flight"]["gate"] = "D31"
        with self.assertRaises(ValueError):
            validate_message(future)


if __name__ == "__main__":
    unittest.main()
