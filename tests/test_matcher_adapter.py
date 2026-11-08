import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from matcher_adapter import Matcher, parking_sessions, resources
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import catalog, initial_gate_state
from test_stream_gates import INBOUND, PARKED, SPARE, assignment, flight


def allocation(payload, placements):
    return {"assignments": [[session["id"], catalog()["footprints"][placements[session["registration"]]]]
                            for session in payload["sessions"] if session["registration"] in placements]}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.algorithm = SimpleNamespace(
            SOLVER_BACKEND=Path("unused.jl"),
            run_julia=Mock(side_effect=lambda backend, payload: allocation(payload, {INBOUND["registration"]: "D31"})),
            repair_assignments_python=Mock(return_value={"assignments": []}),
        )
        self.matcher = Matcher(self.algorithm)
        self.stream = GateState()
        self.outbound = flight("100", "YYZ", "YUL", "06:00", "07:15", PARKED)
        self.inbound = flight("200", "YUL", "YYZ", "07:00", "08:00")
        self.departure = flight("201", "YYZ", "YUL", "09:00", "10:15")

    def initialize(self, flights=None, parked=None, outages=None):
        parked, gates = initial_gate_state([{**PARKED, "gate": "D20"}] if parked is None else parked)
        return self.send({"action": "InitializeAirport", "flights": [self.outbound, self.inbound, self.departure]
                         if flights is None else flights, "parkedAircraft": parked,
                         "gateOutages": outages or [], "gateState": gates}, "2026-11-07T05:00:00Z")

    def send(self, message, timestamp):
        self.stream.apply_message(message, timestamp)
        response = self.matcher.update(self.stream.request(message), timestamp)
        self.stream.apply_decisions(response, initializing=message["action"] == "InitializeAirport")
        self.assertEqual(self.matcher.state.snapshot(), self.stream.snapshot())
        return response

    def test_initial_positions_and_arrival_departure_share_one_plan(self):
        decisions = self.initialize()
        self.assertEqual(set(decisions), {"assignments", "moves"})
        self.assertEqual(decisions["moves"], [])
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.assertIsNone(self.stream.gates["D31"])
        self.assertIn(assignment(self.inbound, "D31"), decisions["assignments"])
        self.assertIn(assignment(self.departure, "D31"), decisions["assignments"])
        payload = self.algorithm.run_julia.call_args.args[1]
        self.assertEqual(len(payload["gates"]), 92)
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertIn(catalog()["footprints"]["D20"], payload["outages"])
        self.stream.advance(self.stream.flights[next(key for key in self.stream.flights if key[1] == "200")]["arrival"])
        self.assertEqual(self.stream.gates["D31"], INBOUND)

    def test_remote_subpositions_share_one_solver_resource(self):
        ids = [gate["id"] for gate in resources()]
        footprint = catalog()["footprints"]["Remote Stand H01A"]
        self.assertEqual(footprint, catalog()["footprints"]["Remote Stand H01"])
        self.assertEqual(ids.count(footprint), 1)
        self.initialize([], [{**PARKED, "gate": "Remote Stand H01A"}])
        self.assertEqual(self.stream.gates["Remote Stand H01A"], PARKED)
        self.assertEqual(self.stream.snapshot()["unassigned_aircraft"], [])

    def test_update_repairs_an_outage_and_moves_a_grounded_aircraft(self):
        self.initialize()
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {PARKED["registration"]: "D22"})
        response = self.send({"action": "GateOutage", "gate": "Gate 120", "scheduledStartTime": "00:30",
                              "scheduledEndTime": "07:00"}, "2026-11-07T05:30:00Z")
        self.assertEqual(response["moves"], [{"registration": PARKED["registration"], "gate": "D22"}])
        self.assertIsNone(self.stream.gates["D20"])
        self.assertEqual(self.stream.gates["D22"], PARKED)
        self.assertEqual(self.algorithm.run_julia.call_count, 1)

    def test_failed_relocation_keeps_the_occupant_and_reports_the_conflict(self):
        self.initialize()
        response = self.send({"action": "GateOutage", "gate": "Gate 120", "scheduledStartTime": "00:30",
                              "scheduledEndTime": "07:00"}, "2026-11-07T05:30:00Z")
        self.assertEqual(response["moves"], [])
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.assertEqual(self.matcher.conflicts, [{"registration": PARKED["registration"], "gate": "D20",
                                                 "reason": "no_immediate_relocation_for_gate_outage"}])

    def test_failed_relocation_reserves_its_old_gate_during_the_second_repair(self):
        earlier = copy.deepcopy(self.inbound)
        earlier["legs"][0].update(scheduledDepartureTime="04:00", scheduledArrivalTime="05:00")
        self.algorithm.run_julia.side_effect = lambda backend, payload: allocation(payload, {INBOUND["registration"]: "D20"})
        def repair(sessions, gates, outages, previous, changed, starts):
            for sid in changed:
                if sessions[sid]["registration"] == PARKED["registration"]:
                    return {"assignments": []}
            self.assertIn([sessions[sid]["start"] - 300, sessions[sid]["start"] + 60],
                          outages[catalog()["footprints"]["D20"]])
            return allocation({"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: "D22"})
        self.algorithm.repair_assignments_python.side_effect = repair
        response = self.initialize([self.outbound, earlier, self.departure], outages=[{
            "action": "GateOutage", "gate": "Gate 120", "scheduledStartTime": "00:30", "scheduledEndTime": "01:00"}])
        self.assertEqual(response["moves"], [])
        self.assertEqual(self.stream.gates["D20"], PARKED)
        self.assertIn(assignment(earlier, "D22"), response["assignments"])

    def test_cancelled_departure_keeps_aircraft_and_blocks_its_gate_to_day_end(self):
        self.initialize()
        self.send({"action": "CancelFlight", "flight": PARKED.copy(), "legs": [assignment(self.outbound, None)["leg"]],
                   "cancelScope": {"type": "SpecificLeg"}}, "2026-11-07T05:30:00Z")
        session = parking_sessions(self.matcher.state)[0]["ground:" + PARKED["registration"]]
        self.assertEqual(session["end"], self.matcher.state.day_end.timestamp() / 60)
        self.assertEqual(self.stream.gates["D20"], PARKED)

    def test_cancel_and_reinstate_between_visits_merge_and_split_reservations(self):
        returning = flight("202", "YUL", "YYZ", "11:00", "12:00")
        later_departure = flight("203", "YYZ", "YUL", "14:00", "15:15")
        self.initialize([self.outbound, self.inbound, self.departure, returning, later_departure])
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: "D22"})
        cancel = {"action": "CancelFlight", "flight": INBOUND.copy(), "legs": [assignment(self.departure, None)["leg"]],
                  "cancelScope": {"type": "SpecificLeg"}}
        response = self.send(cancel, "2026-11-07T06:00:00Z")
        self.assertIn(assignment(returning, "D22"), response["assignments"])
        visits = [s for s in self.matcher.sessions.values() if s["registration"] == INBOUND["registration"]]
        self.assertEqual(len(visits), 1)
        reinstate = {**cancel, "action": "ReinstateFlight"}
        self.send(reinstate, "2026-11-07T07:00:00Z")
        visits = [s for s in self.matcher.sessions.values() if s["registration"] == INBOUND["registration"]]
        self.assertEqual(len(visits), 2)

    def test_timing_update_never_rewrites_past_gate_occupancy(self):
        self.initialize()
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: "D22"})
        delayed = copy.deepcopy(self.departure)
        delayed["action"] = "UpdateTiming"
        delayed["legs"][0]["scheduledDepartureTime"] = "10:00"
        response = self.send(delayed, "2026-11-07T13:30:00Z")
        self.assertEqual(self.stream.gates["D31"], INBOUND)
        self.assertEqual(response["moves"], [])
        self.assertEqual(self.matcher.sessions["ground:" + INBOUND["registration"]]["start"],
                         self.matcher.state.now.timestamp() / 60)

    def test_equipment_change_clears_old_plan_and_assigns_the_new_aircraft(self):
        self.initialize()
        wide = {**SPARE, "aircraftType": "789"}
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {wide["registration"]: "D41"})
        response = self.send({"action": "UpdateEquipment", "flight": wide,
                              "legs": [assignment(self.inbound, None)["leg"]],
                              "equipmentUpdate": {"scope": "AllLegs"}}, "2026-11-07T12:00:00Z")
        self.assertIn(assignment(self.inbound, "D41"), response["assignments"])
        self.assertIsNone(self.stream.gates["D41"])

    def test_invalid_allocator_output_leaves_last_valid_matcher_state(self):
        for result in ({"assignments": [["unknown", "D31"]]},
                       {"assignments": "invalid"},
                       {"assignments": [["unknown", "D31"], ["unknown", "D31"]]}):
            with self.subTest(result=result):
                self.algorithm.run_julia.return_value = result
                self.algorithm.run_julia.side_effect = None
                with self.assertRaises(ValueError):
                    self.initialize()
                self.assertIsNone(self.matcher.state)
                self.stream = GateState()

    def test_solver_cannot_double_book_or_ignore_a_known_outage(self):
        other = flight("300", "YUL", "YYZ", "07:00", "08:00", SPARE)
        self.algorithm.run_julia.side_effect = lambda backend, payload: allocation(
            payload, {INBOUND["registration"]: "D31", SPARE["registration"]: "D31"})
        with self.assertRaisesRegex(ValueError, "bookings overlap"):
            self.initialize([self.inbound, other], [])
        self.assertIsNone(self.matcher.state)
        self.stream = GateState()
        with self.assertRaisesRegex(ValueError, "bookings overlap"):
            self.initialize([self.inbound], [], [{"action": "GateOutage", "gate": "Gate 131", "scheduledStartTime": "07:00"}])
        self.assertIsNone(self.matcher.state)

    def test_waiting_reservation_does_not_enter_early_and_moves_on_next_webhook(self):
        def initial(backend, payload):
            result = allocation(payload, {INBOUND["registration"]: "D31"})
            sid = result["assignments"][0][0]
            result["gate_starts"] = {sid: payload["sessions"][0]["start"] + 30}
            return result

        self.algorithm.run_julia.side_effect = initial
        response = self.initialize()
        self.assertNotIn(assignment(self.inbound, "D31"), response["assignments"])
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: "D31"})
        response = self.send({"action": "GateOutage", "gate": "Gate 122", "scheduledStartTime": "10:00"},
                             "2026-11-07T13:31:00Z")
        self.assertEqual(response["moves"], [{"registration": INBOUND["registration"], "gate": "D31"}])
        self.assertEqual(self.stream.gates["D31"], INBOUND)

    def test_new_day_replaces_state_only_after_successful_solve(self):
        self.initialize()
        previous = self.matcher.state.snapshot()
        self.algorithm.run_julia.side_effect = RuntimeError("solver failed")
        self.stream = GateState()
        with self.assertRaisesRegex(RuntimeError, "solver failed"):
            self.initialize()
        self.assertEqual(self.matcher.state.snapshot(), previous)


if __name__ == "__main__":
    unittest.main()
