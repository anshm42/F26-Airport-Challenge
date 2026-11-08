import json
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from smartgate_stream.replay import MAX_RESPONSE_BYTES, Run, post_message
from smartgate_stream.validation import load_scenario, parse_utc


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scripted.json"


def message_fields(message):
    return {key: value for key, value in message.items() if key not in ("gateState", "unassignedAircraft")}


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
            return 204, None

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        self.assertEqual([s[0] for s in sent], [100, 105, 105])
        self.assertEqual([message_fields(s[1]) for s in sent], [e["message"] for e in self.scenario["events"]])
        self.assertTrue(all(len(s[1]["gateState"]) == 110 for s in sent))
        for index, (_, _, headers) in enumerate(sent):
            self.assertEqual(headers, {"X-Stream-Run-Id": run.id,
                                      "X-Stream-Event-Id": f"{run.id}:{index}",
                                      "X-Stream-Simulated-Time": self.scenario["events"][index]["timestamp"],
                                      "X-Stream-Speed": "60.0"})
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
            return 200, None

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        self.assertEqual(starts, [100, 108, 108])
        self.assertEqual([r["lateness_seconds"] for r in self.records(run)], [0, 3, 3])
        self.assertEqual(run.snapshot()["http_seconds"], 8)
        self.assertEqual(run.snapshot()["lateness_seconds"], 6)

    def test_initialization_must_finish_before_simulation_clock_starts(self):
        original = self.scenario["events"][0]["message"]
        self.scenario["events"][0]["message"] = {
            "action": "InitializeAirport", "flights": [original],
            "parkedAircraft": [{**original["flight"], "gate": "D20"}], "gateOutages": []}
        self.path.write_text(json.dumps(self.scenario))
        for response in (200, 204, 202, 503):
            with self.subTest(response=response):
                clock, sent = Clock(), []
                run = self.run_for_test()

                def send(url, message, headers):
                    sent.append(clock())
                    if len(sent) == 1:
                        clock.now += 8
                        return response, None
                    return 202, None

                run.execute(clock=clock, wait=clock.wait, send=send)
                if response in (200, 204):
                    self.assertEqual(sent, [100, 113, 113])
                    self.assertEqual(run.snapshot()["status"], "completed")
                    self.assertEqual(run.snapshot()["lateness_seconds"], 0)
                    self.assertEqual([r["intended_deadline_seconds"] for r in self.records(run)], [0, 13, 13])
                else:
                    # 503 is retried under the same event ID; the 202 that follows is final.
                    self.assertEqual(sent, [100] if response == 202 else [100, 108.5])
                    self.assertEqual(run.snapshot()["status"], "failed")
                    self.assertEqual(run.snapshot()["delivered"], 0)

    def test_scenario_rate_scales_irregular_gaps_and_preserves_bursts(self):
        scenario = load_scenario(FIXTURE)
        scenario["events"] = scenario["events"][:5]
        start = parse_utc(scenario["events"][0]["timestamp"])
        for event, seconds in zip(scenario["events"], (0, 30, 330, 330, 900)):
            event["timestamp"] = (start + timedelta(seconds=seconds)).isoformat()
        scenario["metadata"] = {"rate": 60}
        self.path.write_text(json.dumps(scenario))
        run = self.run_for_test()
        sent = []

        def send(url, message, headers):
            sent.append((self.clock(), message, headers["X-Stream-Simulated-Time"]))
            return 204, None

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        self.assertEqual([s[0] for s in sent], [100, 100.5, 105.5, 105.5, 115])
        self.assertEqual([message_fields(s[1]) for s in sent], [e["message"] for e in scenario["events"]])
        self.assertEqual([s[2] for s in sent], [e["timestamp"] for e in scenario["events"]])
        self.assertEqual(run.snapshot()["speed"], 60)

    def test_scenario_rate_default_explicit_override_and_unpaced_mode(self):
        self.scenario["metadata"] = {"rate": 120}
        self.path.write_text(json.dumps(self.scenario))
        for options, elapsed, speed in (({}, 2.5, 120), ({"speed": 30}, 10, 30),
                                        ({"mode": "unpaced"}, 0, 120)):
            with self.subTest(options=options):
                clock = Clock()
                run = self.run_for_test(**options)
                run.execute(clock=clock, wait=clock.wait, send=lambda *args: (204, None))
                self.assertEqual(clock.now - 100, elapsed)
                self.assertEqual(run.snapshot()["speed"], speed)

    def test_unpaced_and_custom_speed(self):
        for mode, speed, expected in (("unpaced", 60, 0), ("paced", 300, 1)):
            with self.subTest(mode=mode):
                clock = Clock()
                run = self.run_for_test(mode=mode, speed=speed)
                run.execute(clock=clock, wait=clock.wait, send=lambda *args: (202, None))
                self.assertEqual(clock.now - 100, expected)
                self.assertEqual(run.snapshot()["delivered"], 3)
                self.assertEqual(run.snapshot()["max_lateness_seconds"], 0)

    def test_failed_updates_are_retried_then_skipped(self):
        for result, attempts in ((300, 1), (422, 1), (500, 3), (TimeoutError("timed out"), 3),
                                 (OSError("connection refused"), 3)):
            with self.subTest(result=result):
                run = self.run_for_test(mode="unpaced")
                calls = []

                def send(url, message, headers):
                    calls.append(headers)
                    if isinstance(result, Exception):
                        raise result
                    return result, None

                run.execute(clock=self.clock, wait=self.clock.wait, send=send)
                state = run.snapshot()
                self.assertEqual((state["status"], state["attempted"], state["delivered"]), ("completed", 3, 0))
                self.assertEqual(state["skipped"], 3)
                self.assertEqual(state["skipped_event_ids"], [f"{run.id}:{index}" for index in range(3)])
                self.assertEqual(state["retries"], 3 * (attempts - 1))
                self.assertEqual(len(calls), 3 * attempts)
                # Every attempt of one event reuses its event ID, the matcher's idempotency key.
                first_event = calls[:attempts]
                self.assertEqual({headers["X-Stream-Event-Id"] for headers in first_event}, {f"{run.id}:0"})
                self.assertEqual([headers.get("X-Stream-Attempt") for headers in first_event],
                                 [None, "2", "3"][:attempts])
                self.assertNotIn("X-Stream-Resync", calls[0])
                self.assertEqual(calls[attempts]["X-Stream-Resync"], "true")
                records = self.records(run)
                self.assertTrue(all(record["error"] for record in records))
                self.assertEqual([len(record["attempts"]) for record in records], [attempts] * 3)
                self.assertEqual([event["decision_status"] for event in run.events_after()], ["skipped"] * 3)

    def test_retry_after_timeout_applies_the_response_once_and_clears_resync(self):
        run = self.run_for_test(mode="unpaced")
        calls = []

        def send(url, message, headers):
            calls.append(headers)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return 200, {"assignments": [], "moves": []}

        run.execute(clock=self.clock, wait=self.clock.wait, send=send)
        state = run.snapshot()
        self.assertEqual((state["status"], state["delivered"], state["retries"], state["skipped"]),
                         ("completed", 3, 1, 0))
        self.assertEqual([headers["X-Stream-Event-Id"] for headers in calls],
                         [f"{run.id}:0", f"{run.id}:0", f"{run.id}:1", f"{run.id}:2"])
        self.assertEqual(calls[1]["X-Stream-Attempt"], "2")
        self.assertTrue(all("X-Stream-Resync" not in headers for headers in calls))
        self.assertEqual([len(record["attempts"]) for record in self.records(run)], [2, 1, 1])

    def test_initialization_is_retried_until_the_matcher_listens(self):
        original = self.scenario["events"][0]["message"]
        self.scenario["events"][0]["message"] = {
            "action": "InitializeAirport", "flights": [original],
            "parkedAircraft": [{**original["flight"], "gate": "D20"}], "gateOutages": []}
        self.path.write_text(json.dumps(self.scenario))
        for refusals, status in ((6, "completed"), (100, "failed")):
            with self.subTest(refusals=refusals):
                run = self.run_for_test(mode="unpaced")
                run.initialize_retry_seconds = 20
                calls = []

                def send(url, message, headers):
                    calls.append(message["action"])
                    if len(calls) <= refusals:
                        raise ConnectionRefusedError("connection refused")
                    return 204, None

                clock = Clock()
                run.execute(clock=clock, wait=clock.wait, send=send)
                state = run.snapshot()
                self.assertEqual(state["status"], status)
                if status == "completed":
                    self.assertEqual(calls, ["InitializeAirport"] * 7 + ["UpdateTiming", "GateOutage"])
                else:
                    self.assertEqual(state["delivered"], 0)
                    self.assertTrue(set(calls) == {"InitializeAirport"} and len(calls) > 1)

    def test_stop_interrupts_wait(self):
        run = self.run_for_test()
        calls = []

        def wait(seconds):
            run.stop()

        run.execute(clock=self.clock, wait=wait, send=lambda *args: calls.append(args) or (200, None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.snapshot()["status"], "stopped")
        self.assertFalse(run.stop())

    def test_stop_during_inflight_request(self):
        run = self.run_for_test(mode="unpaced")
        entered, release = threading.Event(), threading.Event()

        def send(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return 200, None

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
        for kwargs in ({"speed": 0}, {"speed": True}, {"speed": None}, {"speed": float("inf")},
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
            self.assertEqual(post_message("http://localhost:9000/updates?run=1", {"action": "test"}, {}), (302, None))
            factory.assert_called_once_with("localhost", 9000, timeout=10)
            self.assertEqual(connection.request.call_args.args[:2], ("POST", "/updates?run=1"))
            self.assertEqual(json.loads(connection.request.call_args.args[2]), {"action": "test"})
            connection.close.assert_called_once()

    def test_matcher_json_is_bounded_and_rejects_ambiguous_responses(self):
        cases = (
            (200, "application/json; charset=utf-8", b'{"assignments":[]}', {"assignments": []}),
            (204, "", b"", None),
            (202, "application/json", b'{"assignments":[]}', ValueError),
            (200, "text/plain", b'{"assignments":[]}', ValueError),
            (200, "application/json", b'{"assignments":[],"assignments":[]}', ValueError),
            (200, "application/json", b'{"value":NaN}', ValueError),
            (200, "application/json", b'{', ValueError),
            (200, "application/json", b' ' * (MAX_RESPONSE_BYTES + 1), ValueError),
        )
        for status, content_type, body, expected in cases:
            with self.subTest(status=status, content_type=content_type, size=len(body)):
                with patch("smartgate_stream.replay.http.client.HTTPConnection") as factory:
                    connection = factory.return_value
                    response = connection.getresponse.return_value
                    response.status = status
                    response.read.return_value = body
                    response.getheader.return_value = content_type
                    if expected is ValueError:
                        with self.assertRaises(ValueError):
                            post_message("http://localhost:9000/updates", {}, {})
                    else:
                        self.assertEqual(post_message("http://localhost:9000/updates", {}, {}), (status, expected))
                    response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
                    connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
