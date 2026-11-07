import copy
import unittest

from smartgate_stream.gates import GateState
from smartgate_stream.inventory import canonical_gate, catalog, gate_fits, initial_gate_state, passenger_aircraft, traffic_type
from smartgate_stream.validation import parse_utc, validate_message


PARKED = {"carrier": "AC", "registration": "C-FFGZ", "aircraftType": "321"}
INBOUND = {"carrier": "AC", "registration": "C-FFIQ", "aircraftType": "321"}
SPARE = {"carrier": "AC", "registration": "C-FABC", "aircraftType": "321"}


def flight(number, origin, destination, departure, arrival, aircraft=INBOUND):
    return {"action": "CreateFlight", "flight": aircraft.copy(), "legs": [{
        "legType": "Originating", "flightNumber": number, "flightSuffix": None,
        "originatingDate": "2026-11-07", "departureStation": origin, "arrivalStation": destination,
        "trafficType": traffic_type(origin, destination),
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
        self.assertEqual(len(state.gates), 110)
        self.assertEqual([plane for plane in state.gates.values() if plane], [PARKED])
        self.assertEqual(canonical_gate("F32"), "D32")
        self.assertNotIn("F32", state.gates)
        self.assertNotIn("Gate 101", state.gates)
        request = state.request(self.arrival)
        self.assertEqual(request["gateState"], state.gates)
        self.assertEqual(request["unassignedAircraft"], [])
        request["gateState"]["D20"] = None
        self.assertEqual(state.gates["D20"], PARKED)

    def test_removed_gates_are_unavailable_and_every_swing_pair_shares_occupancy(self):
        for gate in ("D30", "F82", "F83", "E82", "E85", "B2a", "B2c", "B3", "B4", "B5", "B23"):
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                canonical_gate(gate)
        pairs = [(alias, gate) for alias, gate in catalog()["aliases"].items() if alias != gate]
        self.assertEqual(len(pairs), 15)
        for alias, gate in pairs:
            with self.subTest(alias=alias):
                self.assertEqual(canonical_gate(alias), gate)
                with self.assertRaisesRegex(ValueError, "occupied more than once"):
                    initial_gate_state([{**PARKED, "gate": gate}, {**INBOUND, "gate": alias}])

    def test_ifatc_capacity_applies_to_initial_positions_and_atomic_decisions(self):
        wide = {**INBOUND, "aircraftType": "789"}
        self.assertFalse(gate_fits("D22", wide))
        self.assertTrue(gate_fits("D41", wide))
        self.assertFalse(gate_fits("Remote Stand H01A", wide))
        self.assertTrue(gate_fits("Remote Stand H01", wide))
        self.assertTrue(gate_fits("E71", {**wide, "aircraftType": "388"}))
        with self.assertRaisesRegex(ValueError, "IFATC capacity"):
            initial_gate_state([{**wide, "gate": "D22"}])
        inbound = flight("300", "LHR", "YYZ", "07:00", "10:00", wide)
        state = self.initialized([inbound])
        before = state.snapshot()
        with self.assertRaisesRegex(ValueError, "IFATC capacity"):
            state.apply_decisions({"assignments": [assignment(inbound, "D22")]})
        self.assertEqual(state.snapshot(), before)
        state = self.initialized([], [{**PARKED, "gate": "D20"}, {**wide, "gate": "D41"}])
        before = state.snapshot()
        with self.assertRaisesRegex(ValueError, "IFATC capacity"):
            state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "D31"},
                                              {"registration": wide["registration"], "gate": "D22"}]})
        self.assertEqual(state.snapshot(), before)

    def test_equipment_change_rechecks_planned_gate_capacity_at_arrival(self):
        state = self.initialized([self.arrival], [])
        state.apply_decisions({"assignments": [assignment(self.arrival, "D31")]})
        wide = {**SPARE, "aircraftType": "789"}
        state.apply_message({"action": "UpdateEquipment", "flight": wide,
                             "legs": [assignment(self.arrival, None)["leg"]],
                             "equipmentUpdate": {"scope": "AllLegs"}}, "2026-11-07T12:00:00Z")
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        self.assertIsNone(state.gates["D31"])
        self.assertEqual(state.snapshot()["unassigned_aircraft"], [wide])
        state.apply_decisions({"assignments": [assignment(self.arrival, "D41")]})
        self.assertEqual(state.gates["D41"], wide)

    def test_reference_updates_receive_stored_traffic_type(self):
        arrival = flight("300", "LHR", "YYZ", "07:00", "10:00")
        state = self.initialized([arrival], [])
        message = {"action": "CancelFlight", "flight": INBOUND.copy(),
                   "legs": [assignment(arrival, None)["leg"]], "cancelScope": {"type": "SpecificLeg"}}
        state.apply_message(message, "2026-11-07T06:00:00Z")
        self.assertEqual(state.request(message)["legs"][0]["trafficType"], "International")
        self.assertNotIn("trafficType", message["legs"][0])
        turnback = flight("301", "YYZ", "YYZ", "06:00", "07:00", SPARE)
        turnback.update(action="DivertFlight", emergencyState="Turnback")
        turnback["legs"][0]["trafficType"] = "International"
        state.apply_message(turnback, "2026-11-07T10:30:00Z")
        self.assertEqual(state.request(turnback)["legs"][0]["trafficType"], "International")

    def test_baseline_capacity_counts_all_day_visits_widths_and_overlapping_stands(self):
        for count, code in ((93, "CR9"), (88, "321"), (4, "388")):
            with self.subTest(count=count, code=code):
                flights = [flight(str(300 + index), "YUL", "YYZ", "07:00", "08:00",
                                  {**INBOUND, "registration": f"C-TEST{index}", "aircraftType": code})
                           for index in range(count)]
                state = self.initialized(flights, [])
                before = state.snapshot()
                with self.assertRaisesRegex(ValueError, "baseline schedule exceeds passenger gate capacity"):
                    state.check_baseline_capacity()
                self.assertEqual(state.snapshot(), before)
        state = self.initialized()
        before = state.snapshot()
        state.check_baseline_capacity()
        self.assertEqual(state.snapshot(), before)

    def test_baseline_capacity_reuses_a_gate_at_departure_and_counts_idle_aircraft(self):
        # All three F positions are needed; a same-time departure makes room for the third inbound.
        parked = {**PARKED, "aircraftType": "388"}
        outbound = flight("400", "YYZ", "YUL", "08:00", "09:00", parked)
        incoming = [flight(str(401 + index), "YUL", "YYZ", "07:00", "08:00",
                           {**INBOUND, "registration": f"C-WIDE{index}", "aircraftType": "388"})
                    for index in range(3)]
        state = self.initialized([outbound, *incoming], [{**parked, "gate": "E71"}])
        state.check_baseline_capacity()
        idle = self.initialized(incoming, [{**parked, "gate": "E71"}])
        with self.assertRaisesRegex(ValueError, "baseline schedule exceeds passenger gate capacity"):
            idle.check_baseline_capacity()

    def test_known_initial_outage_reduces_future_baseline_capacity(self):
        incoming = [flight(str(401 + index), "YUL", "YYZ", "07:00", "08:00",
                           {**INBOUND, "registration": f"C-WIDE{index}", "aircraftType": "388"})
                    for index in range(3)]
        state = self.initialized(incoming, [])
        state.apply_message({"action": "GateOutage", "gate": "Gate 171A", "scheduledStartTime": "09:00",
                             "scheduledEndTime": "10:00"}, "2026-11-07T05:00:00Z")
        with self.assertRaisesRegex(ValueError, "2026-11-07T14:00:00Z"):
            state.check_baseline_capacity()

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
        state.apply_message({"action": "GateOutage", "gate": "Gate 122", "scheduledStartTime": "00:00",
                             "scheduledEndTime": "10:00"}, "2026-11-07T05:01:00Z")
        state.apply_decisions({"assignments": [assignment(self.arrival, "D22")]})
        with self.assertRaises(ValueError):
            state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "D22"}]})
        state.advance(parse_utc("2026-11-07T13:00:00Z"))
        self.assertIsNone(state.gates["D22"])
        self.assertEqual(state.snapshot()["unassigned_aircraft"], [INBOUND])

    def test_ifatc_outages_block_shared_resources_only_during_the_window(self):
        for stand, gate in (("Gate 132", "F32"), ("Gate B11A", "A11"),
                            ("Remote Stand H01A", "Remote Stand H01B")):
            with self.subTest(stand=stand):
                state = self.initialized([])
                outage = {"action": "GateOutage", "gate": stand, "scheduledStartTime": "08:00",
                          "scheduledEndTime": "10:00"}
                state.apply_message(outage, "2026-11-07T12:30:00Z")
                move = {"moves": [{"registration": PARKED["registration"], "gate": gate}]}
                state.apply_decisions(move)
                state.apply_decisions({"moves": [{"registration": PARKED["registration"], "gate": "D20"}]})
                state.advance(parse_utc("2026-11-07T13:00:00Z"))
                before = state.snapshot()
                with self.assertRaisesRegex(ValueError, "out-of-service"):
                    state.apply_decisions(move)
                self.assertEqual(state.snapshot(), before)
                self.assertEqual(state.request(outage)["gate"], stand)
                state.advance(parse_utc("2026-11-07T15:00:00Z"))
                state.apply_decisions(move)
                self.assertEqual(state.gates[canonical_gate(gate)], PARKED)

    def test_initial_ifatc_outage_rejects_an_occupied_shared_gate(self):
        initial = {"action": "InitializeAirport", "flights": [],
                   "parkedAircraft": [{**PARKED, "gate": "D20"}], "gateOutages": [
                       {"action": "GateOutage", "gate": "Gate 120A", "scheduledStartTime": "00:00"}]}
        with self.assertRaisesRegex(ValueError, "initial aircraft occupies an out-of-service position"):
            GateState().apply_message(initial, "2026-11-07T05:00:00Z")

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

    def test_replacement_preserves_each_completed_leg_in_a_connected_route(self):
        route = copy.deepcopy(self.departure)
        returning = flight("100", "YUL", "YYZ", "08:00", "09:00", PARKED)["legs"][0]
        returning["legType"] = "Through"
        route["legs"].append(returning)
        state = self.initialized([route])
        state.apply_decisions({"assignments": [assignment({"flight": route["flight"], "legs": [returning]}, "D22")]})
        state.advance(parse_utc("2026-11-07T11:00:00Z"))
        self.assertIsNone(state.gates["D20"])
        replacement = copy.deepcopy(route)
        replacement["action"] = "ReplaceFlight"
        replacement["legs"][1]["scheduledArrivalTime"] = "09:15"
        state.apply_message(replacement, "2026-11-07T11:30:00Z")
        state.advance(parse_utc("2026-11-07T14:15:00Z"))
        self.assertEqual(state.gates["D22"], PARKED)

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
