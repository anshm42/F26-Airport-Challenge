import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from smartgate_stream.replay import Run, post_message
from smartgate_stream.validation import load_scenario


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scripted.json"


class Clock:
    def __init__(self):
        self.now = 100
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scenario.json"
        self.scenario = load_scenario(FIXTURE)
        self.scenario["events"] = self.scenario["events"][:3]
        self.path.write_text(json.dumps(self.scenario))
        self.clock = Clock()

    def run_for_test(self, **kwargs):
        run = Run(self.path, "http://localhost:9000/updates", **kwargs)
        self.addCleanup(shutil.rmtree, run.directory)
        return run

    def records(self, run):
        return [json.loads(line) for line in (run.directory / "deliveries.jsonl").read_text().splitlines()]

    def test_pacing_equal_times_order_headers_and_snapshot(self):
        run = self.run_for_test()
        sent = []

        def send(url, message, headers):
            sent.append((self.clock(), message, headers))
            return 204

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        self.assertEqual([s[0] for s in sent], [100, 105, 105])
        self.assertEqual([s[1] for s in sent], [e["message"] for e in self.scenario["events"]])
        for index, (_, _, headers) in enumerate(sent):
            self.assertEqual(headers, {"X-Stream-Run-Id": run.id,
                                      "X-Stream-Event-Id": f"{run.id}:{index}",
                                      "X-Stream-Simulated-Time": self.scenario["events"][index]["timestamp"]})
        self.assertEqual(run.snapshot()["status"], "completed")
        self.assertEqual(run.snapshot()["delivered"], 3)
        self.assertEqual(run.snapshot()["simulated_time"], "2026-11-07T12:35:00Z")
        self.assertEqual(json.loads((run.directory / "scenario.json").read_text()), self.scenario)
        self.assertEqual([r["intended_deadline_seconds"] for r in self.records(run)], [0, 5, 5])
        self.assertEqual(run.snapshot()["elapsed_seconds"], 5)

    def test_slow_ack_does_not_shift_deadlines(self):
        run = self.run_for_test()
        starts = []

        def send(*args):
            starts.append(self.clock())
            if len(starts) == 1:
                self.clock.now += 8
            return 200

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        self.assertEqual(starts, [100, 108, 108])
        self.assertEqual([r["lateness_seconds"] for r in self.records(run)], [0, 3, 3])
        self.assertEqual(run.snapshot()["http_seconds"], 8)
        self.assertEqual(run.snapshot()["lateness_seconds"], 6)

    def test_unpaced_and_custom_speed(self):
        for mode, speed, expected in (("unpaced", 60, 0), ("paced", 300, 1)):
            with self.subTest(mode=mode):
                clock = Clock()
                run = self.run_for_test(mode=mode, speed=speed)
                run.execute(clock=clock, wait=clock.wait, send=lambda *args: 202)
                self.assertEqual(clock.now - 100, expected)
                self.assertEqual(run.snapshot()["delivered"], 3)
                self.assertEqual(run.snapshot()["max_lateness_seconds"], 0)

    def test_failures_stop_delivery_and_are_recorded(self):
        for result in (300, 500, TimeoutError("timed out"), OSError("connection refused")):
            with self.subTest(result=result):
                run = self.run_for_test(mode="unpaced")
                calls = []

                def send(*args):
                    calls.append(args)
                    if isinstance(result, Exception):
                        raise result
                    return result

                run.execute(send=send)
                state = run.snapshot()
                self.assertEqual((state["status"], state["attempted"], state["delivered"]), ("failed", 1, 0))
                self.assertEqual(state["failed_event_id"], f"{run.id}:0")
                self.assertTrue(state["error"])
                self.assertEqual(len(calls), 1)
                self.assertTrue(self.records(run)[0]["error"])

    def test_stop_interrupts_wait(self):
        run = self.run_for_test()
        calls = []

        def wait(seconds):
            run.stop()

        run.execute(clock=self.clock, wait=wait, send=lambda *args: calls.append(args) or 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.snapshot()["status"], "stopped")
        self.assertFalse(run.stop())

    def test_stop_during_inflight_request(self):
        run = self.run_for_test(mode="unpaced")
        entered, release = threading.Event(), threading.Event()

        def send(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return 200

        worker = threading.Thread(target=run.execute, kwargs={"send": send})
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(run.snapshot()["attempted"], 1)
            self.assertTrue(run.stop())
            self.assertEqual(run.snapshot()["status"], "stopping")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(run.snapshot()["status"], "stopped")
        self.assertEqual(run.snapshot()["delivered"], 1)

    def test_invalid_configuration_and_fixture_create_no_records(self):
        for kwargs in ({"speed": 0}, {"speed": True}, {"speed": float("inf")},
                       {"speed": "60"}, {"speed": 10**400}, {"speed": 5e-324}, {"mode": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_for_test(**kwargs)
        for url in ("file:///etc/passwd", "http://", "http://user:pass@host", "http://host/#fragment",
                    "http://host:0", "http://host:99999", "http://host\n/path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Run(self.path, url)
        self.scenario["events"][-1]["message"]["scheduledStartTime"] = "25:00"
        self.path.write_text(json.dumps(self.scenario))
        with patch("smartgate_stream.replay.tempfile.mkdtemp") as mkdtemp:
            with self.assertRaises(ValueError):
                self.run_for_test()
            mkdtemp.assert_not_called()

    def test_webhook_client_uses_timeout_and_does_not_follow_redirects(self):
        with patch("smartgate_stream.replay.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.return_value.status = 302
            self.assertEqual(post_message("http://localhost:9000/updates?run=1", {"action": "test"}, {}), 302)
            factory.assert_called_once_with("localhost", 9000, timeout=10)
            self.assertEqual(connection.request.call_args.args[:2], ("POST", "/updates?run=1"))
            self.assertEqual(json.loads(connection.request.call_args.args[2]), {"action": "test"})
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
