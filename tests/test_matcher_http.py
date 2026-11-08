import contextlib
import copy
import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from matcher_adapter import MatcherServer, load_algorithm
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import initial_gate_state
from smartgate_stream.replay import Run
from test_matcher_adapter import allocation
from test_stream_gates import INBOUND, PARKED, flight


class MatcherHttpTests(unittest.TestCase):
    def setUp(self):
        self.algorithm = SimpleNamespace(SOLVER_BACKEND=Path("unused.jl"),
            run_julia=Mock(side_effect=lambda backend, payload: allocation(payload, {INBOUND["registration"]: "D31"})),
            repair_assignments_python=Mock(return_value={"assignments": []}))
        self.server = MatcherServer(self.algorithm, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.stream = GateState()
        self.inbound = flight("200", "YUL", "YYZ", "07:00", "08:00")
        self.departure = flight("201", "YYZ", "YUL", "09:00", "10:15")
        parked, gates = initial_gate_state([{**PARKED, "gate": "D20"}])
        self.initial = {"action": "InitializeAirport", "flights": [self.inbound, self.departure],
                        "parkedAircraft": parked, "gateOutages": [], "gateState": gates}
        self.stream.apply_message(self.initial, "2026-11-07T05:00:00Z")
        self.initial = self.stream.request(self.initial)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, index=0, run="test-run", timestamp="2026-11-07T05:00:00Z", raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json", "X-Stream-Run-Id": run,
                   "X-Stream-Event-Id": f"{run}:{index}", "X-Stream-Simulated-Time": timestamp} if headers is None else headers
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                connection.request(method, path, raw if raw is not None else json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_stream_replay_applies_future_plans_and_current_moves_over_http(self):
        choices = iter(("D22", "D31"))
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: next(choices)})
        scenario = {"events": [{"timestamp": "2026-11-07T05:00:00Z", "message": self.initial},
            {"timestamp": "2026-11-07T12:50:00Z", "message": {"action": "GateOutage", "gate": "Gate 131",
                "scheduledStartTime": "07:55", "scheduledEndTime": "08:10"}},
            {"timestamp": "2026-11-07T13:30:00Z", "message": {"action": "GateOutage", "gate": "Gate 122",
                "scheduledStartTime": "08:30", "scheduledEndTime": "10:00"}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_text(json.dumps(scenario))
            run = Run(path, f"http://127.0.0.1:{self.server.server_port}/updates", mode="unpaced")
            self.addCleanup(shutil.rmtree, run.directory)
            with contextlib.redirect_stdout(io.StringIO()):
                run.execute()
            status = run.snapshot()
            self.assertEqual(status["status"], "completed", status["error"])
            self.assertEqual(status["delivered"], 3)
            records = [json.loads(line) for line in (run.directory / "deliveries.jsonl").read_text().splitlines()]
            self.assertEqual([record["http_status"] for record in records], [200, 200, 200])
            self.assertEqual(records[-1]["matcher_response"]["moves"], [{"registration": INBOUND["registration"], "gate": "D31"}])
            self.assertEqual(set(records[0]["matcher_response"]), {"assignments", "moves"})
            self.assertEqual(self.algorithm.run_julia.call_count, 1)
            self.assertEqual(self.request("GET", "/health")[1]["last_event_id"], run.id + ":2")
            observed = self.request("GET", "/assignments")[1]
            self.assertEqual(observed["gate_state"]["D31"], INBOUND)
            self.assertEqual(observed["gate_state_time"], "2026-11-07T13:30:00Z")

    def test_order_and_run_headers_prevent_duplicate_state_changes(self):
        self.assertEqual(self.request("POST", "/updates", self.initial, index=1)[0], 409)
        status, decisions = self.request("POST", "/updates", self.initial)
        self.assertEqual(status, 200)
        self.stream.apply_decisions(decisions, initializing=True)
        before = self.server.matcher.state.snapshot()
        self.assertEqual(self.request("POST", "/updates", self.initial)[0], 409)
        self.assertEqual(self.request("POST", "/updates", {"action": "GateOutage"}, index=2)[0], 409)
        self.assertEqual(self.request("POST", "/updates", {"action": "GateOutage"}, index=1, run="other-run")[0], 409)
        self.assertEqual(self.server.matcher.state.snapshot(), before)
        self.assertEqual(self.server.last_index, 0)

    def test_failed_solve_and_failed_update_keep_last_successful_event(self):
        status, decisions = self.request("POST", "/updates", self.initial)
        self.assertEqual(status, 200)
        self.stream.apply_decisions(decisions, initializing=True)
        before = self.server.matcher.state.snapshot()
        self.algorithm.run_julia.side_effect = RuntimeError("solver unavailable")
        self.assertEqual(self.request("POST", "/updates", self.initial, run="new-run")[0], 500)
        self.assertEqual(self.server.run_id, "test-run")
        outage = {"action": "GateOutage", "gate": "Gate 131", "scheduledStartTime": "07:55"}
        timestamp = "2026-11-07T12:50:00Z"
        self.stream.apply_message(outage, timestamp)
        request = self.stream.request(outage)
        self.algorithm.repair_assignments_python.side_effect = RuntimeError("repair failed")
        self.assertEqual(self.request("POST", "/updates", request, index=1, timestamp=timestamp)[0], 500)
        self.assertEqual(self.server.matcher.state.snapshot(), before)
        self.assertEqual(self.server.last_index, 0)
        self.algorithm.repair_assignments_python.side_effect = lambda sessions, gates, outages, previous, changed, starts: allocation(
            {"sessions": [sessions[sid] for sid in changed]}, {INBOUND["registration"]: "D22"})
        self.assertEqual(self.request("POST", "/updates", request, index=1, timestamp=timestamp)[0], 200)
        self.assertEqual(self.server.last_index, 1)

    def test_request_validation_rejects_ambiguous_json_and_missing_time(self):
        self.assertEqual(self.request("POST", "/updates", self.initial, headers={"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/updates", self.initial, headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/updates", raw='{"action":"InitializeAirport","action":"GateOutage"}')[0], 400)
        self.assertEqual(self.request("POST", "/updates", self.initial, timestamp="2026-11-07T00:00:00")[0], 400)
        wrong_occupancy = copy.deepcopy(self.initial)
        wrong_occupancy["unassignedAircraft"] = [INBOUND]
        self.assertEqual(self.request("POST", "/updates", wrong_occupancy)[0], 400)
        self.assertIsNone(self.server.matcher.state)
        self.algorithm.run_julia.assert_not_called()

    def test_loader_reports_an_incompatible_partner_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "algorithm.py"
            path.write_text("SOLVER_BACKEND = 'unused.jl'\n")
            with self.assertRaisesRegex(ValueError, "must provide run_julia"):
                load_algorithm(path)


if __name__ == "__main__":
    unittest.main()
