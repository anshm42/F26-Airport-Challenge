"""Licensed solver regressions; run with RUN_GUROBI_TESTS=1."""

import os
import unittest

from matcher_server import JuliaWorker, SOLVER_BACKEND, build_solver_payload
from run_local import find_julia


@unittest.skipUnless(os.environ.get("RUN_GUROBI_TESTS") == "1", "requires RUN_GUROBI_TESTS=1 and licensed Julia/Gurobi")
class SolverOccupancyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = JuliaWorker(SOLVER_BACKEND, find_julia(), timeout=30)
        cls.addClassCleanup(cls.worker.process.stdout.close)
        cls.addClassCleanup(cls.worker.process.stdin.close)
        cls.addClassCleanup(cls.worker.close)

    def setUp(self):
        self.sessions = {
            "parked": {"id": "parked", "registration": "C-PARKED", "widthClass": "C",
                       "start": 0, "end": 120, "initiallyParked": True, "fixedGate": "D28"},
            "arrival-1": {"id": "arrival-1", "registration": "C-INBOUND1", "widthClass": "C", "start": 10, "end": 30},
            "arrival-2": {"id": "arrival-2", "registration": "C-INBOUND2", "widthClass": "C", "start": 40, "end": 60},
        }
        self.gates = [{"id": "D28", "conflictGroup": "D28", "widthClass": "C", "kind": "gate"}]

    def solve(self):
        return self.worker.run({"mode": "initial", **build_solver_payload(self.sessions, self.gates, {})})

    def test_parked_aircraft_cannot_be_dropped_for_two_future_arrivals(self):
        result = self.solve()
        assignments = dict(result["assignments"])
        self.assertEqual(assignments.get("parked"), "D28",
                         f"A parked aircraft must retain its occupied gate; future arrivals must wait instead. Received {result}")
        self.assertCountEqual(result["unassigned"], ["arrival-1", "arrival-2"])

    def test_arrivals_use_another_compatible_gate(self):
        self.gates.append({**self.gates[0], "id": "D30", "conflictGroup": "D30"})
        result = self.solve()
        self.assertEqual(dict(result["assignments"]),
                         {"parked": "D28", "arrival-1": "D30", "arrival-2": "D30"})
        self.assertEqual(result["unassigned"], [])

    def test_departure_releases_gate_at_end_of_occupied_session(self):
        self.sessions["parked"]["end"] = 10
        result = self.solve()
        self.assertEqual(dict(result["assignments"]),
                         {sid: "D28" for sid in self.sessions})
        self.assertEqual(result["unassigned"], [])

    def test_shared_conflict_group_is_still_occupied(self):
        self.gates.append({**self.gates[0], "id": "D28A"})
        result = self.solve()
        self.assertEqual(dict(result["assignments"]), {"parked": "D28"})
        self.assertCountEqual(result["unassigned"], ["arrival-1", "arrival-2"])

    def test_invalid_mandatory_reservation_is_infeasible(self):
        self.sessions["parked"]["widthClass"] = "F"
        with self.assertRaisesRegex(RuntimeError, "no feasible solution"):
            self.solve()

    def test_relocatable_occupant_is_still_mandatory(self):
        parked = self.sessions["parked"]
        parked.pop("fixedGate")
        parked.update(occupiedGate="D28", departureTrafficType="International")
        self.gates[0]["allowedTrafficTypes"] = ["Domestic"]
        self.gates.append({**self.gates[0], "id": "E70", "conflictGroup": "E70",
                           "allowedTrafficTypes": ["International"]})
        self.assertEqual(dict(self.solve()["assignments"])["parked"], "E70")
        self.gates.pop()
        with self.assertRaisesRegex(RuntimeError, "no feasible solution"):
            self.solve()


if __name__ == "__main__":
    unittest.main()
