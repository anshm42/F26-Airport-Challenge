import contextlib
import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from smartgate_harness.runner import Harness
from smartgate_naive.server import NaiveServer
from smartgate_stream.server import ControlServer


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pearson-400-flights-seed42.json"
EVENTS = 40


def serve(test, server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server.server_port


class HarnessTest(unittest.TestCase):
    def algorithm(self):
        naive = serve(self, NaiveServer(("127.0.0.1", 0)))
        stream = serve(self, ControlServer(0))
        return {"webhook_url": f"http://127.0.0.1:{naive}/updates",
                "websocket_url": f"ws://127.0.0.1:{naive}/passenger/ws",
                "health_url": f"http://127.0.0.1:{naive}/health",
                "stream_url": f"http://127.0.0.1:{stream}"}

    def test_parallel_runs_record_response_and_feed_timing(self):
        output = Path(tempfile.mkdtemp())
        scenario = json.loads(FIXTURE.read_text())
        scenario["events"] = scenario["events"][:EVENTS]
        fixture = output / "short.json"
        fixture.write_text(json.dumps(scenario))
        config = {
            "output_dir": str(output), "parallel": True, "run_timeout_seconds": 120,
            "algorithms": {"first": self.algorithm(), "second": self.algorithm(),
                           "absent": {"webhook_url": "http://127.0.0.1:1/updates",
                                      "websocket_url": "ws://127.0.0.1:1/passenger/ws",
                                      "health_url": "http://127.0.0.1:1/health", "stream_url": "http://127.0.0.1:1"}},
            "startup_timeout_seconds": 1,
            "runs": [{"name": "small", "scenario": str(fixture), "mode": "unpaced"}],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            session = Harness(config).run()
        rows = {row["algorithm"]: row for row in json.loads((session / "summary.json").read_text())}
        self.assertEqual(rows["absent"]["status"], "unavailable")
        for name in ("first", "second"):
            row = rows[name]
            self.assertEqual(row["status"], "completed", row)
            self.assertEqual((row["flights"], row["total_events"], row["events_heard"]), (400, EVENTS, EVENTS))
            self.assertGreater(row["init_response_seconds"], 0)
            self.assertGreater(row["init_feed_seconds"], 0)
            with (session / "runs" / "small" / name / "events.csv").open() as source:
                events = list(csv.DictReader(source))
            self.assertEqual(len(events), EVENTS)
            self.assertEqual(events[0]["action"], "InitializeAirport")
            self.assertEqual(events[0]["assignments"], "400")
            self.assertTrue(all(event["http_status"] == "200" for event in events))
            self.assertEqual(sum(not event["feed_latency_seconds"] for event in events), row["updates_without_feed"])
        with (session / "summary.csv").open() as source:
            self.assertEqual(len(list(csv.DictReader(source))), 3)


if __name__ == "__main__":
    unittest.main()
