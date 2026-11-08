import contextlib
import io
import json
import socket
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import matcher_server as matcher
import run_local
from smartgate_stream.inventory import initial_gate_state
from smartgate_stream.server import ControlServer
from test_stream_gates import PARKED, flight


class StopAfterReplay(io.StringIO):
    def write(self, value):
        if value.startswith("Replay complete:"):
            raise KeyboardInterrupt
        return super().write(value)


class LocalRunnerTests(unittest.TestCase):
    def test_invalid_speed_is_rejected_before_starting_services(self):
        for speed in ("0", "-1", "nan", "inf"):
            with self.subTest(speed=speed), patch.object(run_local, "ControlServer") as control, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    run_local.main(["--speed", speed])
                self.assertEqual(error.exception.code, 2)
                control.assert_not_called()

    def test_launcher_runs_paced_replay_and_closes_owned_services(self):
        self.exercise_launcher("paced")

    def test_launcher_accepts_an_unpaced_scenario(self):
        self.exercise_launcher("unpaced")

    def exercise_launcher(self, mode):
        parked, gates = initial_gate_state([{**PARKED, "gate": "D20"}])
        initial = {"action": "InitializeAirport", "flights": [flight("100", "YYZ", "YUL", "05:01", "06:01", PARKED)],
                   "parkedAircraft": parked, "gateOutages": [], "gateState": gates}
        scenario = {"events": [{"timestamp": "2026-11-07T10:00:00Z", "message": initial},
            {"timestamp": "2026-11-07T10:02:00Z", "message": {
                "action": "GateOutage", "gate": "Gate 131", "scheduledStartTime": "05:02"}}]}
        worker = Mock()
        worker.run.side_effect = lambda payload: {"assignments": [[s["id"], "D20"] for s in payload["sessions"]]}
        servers = []

        def control(_):
            server = ControlServer(0)
            servers.append(server)
            return server

        def receiver(address, handler):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            servers.append(server)
            return server

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            path = Path(directory) / "scenario.json"
            path.write_text(json.dumps(scenario))
            stack.enter_context(patch.dict(vars(matcher)))
            stack.enter_context(patch.object(run_local, "find_julia", return_value="julia"))
            stack.enter_context(patch.object(run_local, "ControlServer", side_effect=control))
            stack.enter_context(patch.object(run_local, "ThreadingHTTPServer", side_effect=receiver))
            stack.enter_context(patch.object(matcher, "JuliaWorker", return_value=worker))
            stack.enter_context(contextlib.redirect_stdout(StopAfterReplay()))
            self.assertEqual(run_local.main(["--scenario", str(path), "--mode", mode, "--speed", "120"]), 0)
            run = next(iter(servers[0].manager.runs.values()))
            status = run.snapshot()
            self.assertEqual((status["mode"], status["speed"], status["status"], status["delivered"]),
                             (mode, 120, "completed", 2))
            if mode == "paced":
                self.assertGreaterEqual(status["elapsed_seconds"], 1)
            self.assertTrue((run.directory / "status.json").is_file())
            self.assertEqual(len(worker.run.call_args_list[0].args[0]["sessions"]), 2)
            worker.close.assert_called_once()
            self.assertIsNone(matcher.JULIA_WORKER)
            for server in servers:
                with socket.socket() as probe:
                    probe.bind(server.server_address)

    def test_interrupted_worker_startup_closes_the_julia_process(self):
        with patch.object(matcher.JuliaWorker, "start", side_effect=KeyboardInterrupt), \
             patch.object(matcher.JuliaWorker, "close") as close:
            with self.assertRaises(KeyboardInterrupt):
                matcher.JuliaWorker(Path("unused.jl"), "julia", 9)
            close.assert_called_once()

    def test_busy_port_fails_without_starting_a_worker(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with patch.object(run_local, "ControlServer", side_effect=lambda _: ControlServer(occupied.getsockname()[1])), \
                 patch.object(matcher, "JuliaWorker") as worker, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_local.main([]), 1)
            worker.assert_not_called()
            self.assertGreater(occupied.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
