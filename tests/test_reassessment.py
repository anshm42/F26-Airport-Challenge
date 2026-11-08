import json
import shutil
import tempfile
import unittest
from pathlib import Path

from smartgate_stream.replay import Run
from test_stream_gates import INBOUND, PARKED, assignment, flight
from test_stream_replay import Clock


class ReassessmentTests(unittest.TestCase):
    def run_scenario(self, send, extra=(), outages=()):
        initial = {"action": "InitializeAirport", "flights": [
            flight("100", "YYZ", "YUL", "06:00", "07:00", PARKED),
            flight("200", "YUL", "YYZ", "04:00", "05:30")],
            "parkedAircraft": [{**PARKED, "gate": "D20"}], "gateOutages": list(outages)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_text(json.dumps({"events": [
                {"timestamp": "2026-11-07T10:00:00Z", "message": initial}, *extra]}))
            run = Run(path, "http://localhost/updates", mode="unpaced")
        self.addCleanup(shutil.rmtree, run.directory)
        clock = Clock()
        run.execute(clock=clock, wait=clock.wait, send=send)
        return run

    def test_waiting_aircraft_gets_gate_after_departure_without_flight_update(self):
        requests = []
        def send(url, message, headers):
            requests.append((message, headers))
            moves = []
            if message["action"] == "ReassessGates" and headers["X-Stream-Simulated-Time"] == "2026-11-07T11:00:00Z":
                self.assertIsNone(message["gateState"]["D20"])
                self.assertEqual(message["unassignedAircraft"], [INBOUND])
                moves = [{"registration": INBOUND["registration"], "gate": "D20"}]
            return 200, {"moves": moves, "reassessment": {"at": None}}
        run = self.run_scenario(send)
        self.assertEqual(run.snapshot()["status"], "completed")
        self.assertEqual(run.gate_state.gates["D20"], INBOUND)
        self.assertEqual((run.state["delivered"], run.state["reassessments_delivered"]), (1, 2))
        self.assertEqual([h["X-Stream-Event-Id"] for _, h in requests], [f"{run.id}:{i}" for i in range(3)])

    def test_timer_replaced_by_update_and_equal_time_movement_uses_update(self):
        seen = []
        def send(url, message, headers):
            seen.append((message["action"], headers["X-Stream-Simulated-Time"]))
            return 200, {"reassessment": {"at": "2026-11-07T10:20:00Z" if len(seen) == 1 else None}}
        update = {"timestamp": "2026-11-07T10:10:00Z", "message": {
            "action": "GateOutage", "gate": "Gate 122", "scheduledStartTime": "05:30", "scheduledEndTime": "06:10"}}
        run = self.run_scenario(send, [update])
        self.assertEqual(run.state["status"], "completed")
        self.assertNotIn(("ReassessGates", "2026-11-07T10:20:00Z"), seen)
        self.assertEqual(seen.count(("ReassessGates", "2026-11-07T10:30:00Z")), 1)
        self.assertIn(("ReassessGates", "2026-11-07T11:10:00Z"), seen)

    def test_delayed_entry_timer_and_outage_reopening(self):
        seen = []
        def send(url, message, headers):
            timestamp = headers["X-Stream-Simulated-Time"]
            seen.append(timestamp)
            return 200, {"reassessment": {"at": "2026-11-07T10:45:00Z" if len(seen) == 1 else None}}
        run = self.run_scenario(send, outages=[{"action": "GateOutage", "gate": "Gate 122",
            "scheduledStartTime": "05:00", "scheduledEndTime": "05:45"}])
        self.assertEqual(run.state["status"], "completed")
        self.assertEqual(seen.count("2026-11-07T10:45:00Z"), 1)

    def test_bad_timer_fails_atomically(self):
        for at in ("2026-11-07T10:00:00Z", "yesterday", 12):
            with self.subTest(at=at):
                run = self.run_scenario(lambda *args: (200, {"reassessment": {"at": at},
                    "moves": [{"registration": PARKED["registration"], "gate": "D22"}]}))
                self.assertEqual(run.state["status"], "failed")
                self.assertEqual(run.gate_state.gates["D20"], PARKED)
                self.assertEqual(run.state["attempted"], 1)
