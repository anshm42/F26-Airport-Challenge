"""Licensed solver regressions; run with RUN_GUROBI_TESTS=1."""

import os
import unittest

from matcher_server import JuliaWorker, SOLVER_BACKEND, build_solver_payload
from run_local import find_julia


@unittest.skipUnless(os.environ.get("RUN_GUROBI_TESTS") == "1", "requires RUN_GUROBI_TESTS=1 and licensed Julia/Gurobi")
class SolverOccupancyTests(unittest.TestCase):
    def test_parked_aircraft_cannot_be_dropped_for_two_future_arrivals(self):
        sessions = {
            "parked": {"id": "parked", "registration": "C-PARKED", "widthClass": "C",
                       "start": 0, "end": 120, "initiallyParked": True, "fixedGate": "D28"},
            "arrival-1": {"id": "arrival-1", "registration": "C-INBOUND1", "widthClass": "C", "start": 10, "end": 30},
            "arrival-2": {"id": "arrival-2", "registration": "C-INBOUND2", "widthClass": "C", "start": 40, "end": 60},
        }
        gates = [{"id": "D28", "conflictGroup": "D28", "widthClass": "C", "kind": "gate"}]
        worker = JuliaWorker(SOLVER_BACKEND, find_julia(), timeout=30)
        self.addCleanup(worker.process.stdout.close)
        self.addCleanup(worker.process.stdin.close)
        self.addCleanup(worker.close)
        result = worker.run({"mode": "initial", **build_solver_payload(sessions, gates, {})})
        assignments = dict(result["assignments"])
        self.assertEqual(assignments.get("parked"), "D28",
                         f"A parked aircraft must retain its occupied gate; future arrivals must wait instead. Received {result}")
        self.assertCountEqual(result["unassigned"], ["arrival-1", "arrival-2"])


if __name__ == "__main__":
    unittest.main()
