import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smartgate_stream.generate import generate
from smartgate_stream.server import ControlServer
from smartgate_stream.validation import load_scenario


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "scripted.json"


class Webhook(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append((message, dict(self.headers)))
        self.server.entered.set()
        if not self.server.release.wait(3):
            self.send_error(504)
            return
        self.send_response(self.server.response_status)
        self.end_headers()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.webhook = ThreadingHTTPServer(("127.0.0.1", 0), Webhook)
        self.webhook.received = []
        self.webhook.entered = threading.Event()
        self.webhook.release = threading.Event()
        self.webhook.release.set()
        self.webhook.response_status = 204
        self.control = ControlServer(0)
        self.threads = []
        for server in (self.webhook, self.control):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.addCleanup(self.close_servers)
        self.options = {"scenario_path": str(FIXTURE),
                        "webhook_url": f"http://127.0.0.1:{self.webhook.server_port}/updates",
                        "mode": "unpaced"}

    def close_servers(self):
        self.webhook.release.set()
        self.control.shutdown()
        self.control.server_close()
        self.webhook.shutdown()
        self.webhook.server_close()
        for thread in self.threads:
            thread.join(2)
        for run in self.control.manager.runs.values():
            shutil.rmtree(run.directory)

    def request(self, method, path, body=None, *, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.control.server_port, timeout=3)
        try:
            data = raw if raw is not None else (json.dumps(body) if body is not None else None)
            connection.request(method, path, data, headers if headers is not None else {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def terminal(self, run_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, result = self.request("GET", f"/runs/{run_id}")
            self.assertEqual(status, 200)
            if result["status"] not in ("running", "stopping"):
                return result
            time.sleep(0.005)
        self.fail("run did not finish")

    def test_end_to_end_unchanged_messages_headers_and_records(self):
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("completed", 8))
        expected = load_scenario(FIXTURE)["events"]
        self.assertEqual([r[0] for r in self.webhook.received], [e["message"] for e in expected])
        for index, (_, headers) in enumerate(self.webhook.received):
            headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(headers["x-stream-event-id"], f"{state['run_id']}:{index}")
            self.assertEqual(headers["x-stream-simulated-time"], expected[index]["timestamp"])
        records = Path(state["records_directory"]) / "deliveries.jsonl"
        self.assertEqual(len(records.read_text().splitlines()), 8)
        self.assertEqual(self.request("POST", f"/runs/{state['run_id']}/stop")[0], 409)
        code, second = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        self.assertNotEqual(second["run_id"], state["run_id"])
        self.terminal(second["run_id"])
        self.assertEqual(self.request("GET", f"/runs/{state['run_id']}")[1]["delivered"], 8)

    def test_overlap_and_stop_inflight(self):
        self.webhook.release.clear()
        _, created = self.request("POST", "/runs", self.options)
        self.assertTrue(self.webhook.entered.wait(2))
        self.assertEqual(self.request("POST", "/runs", self.options)[0], 409)
        code, state = self.request("POST", f"/runs/{created['run_id']}/stop")
        self.assertEqual((code, state["status"]), (202, "stopping"))
        self.assertEqual(self.request("POST", "/runs", self.options)[0], 409)
        self.webhook.release.set()
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("stopped", 1))
        self.assertEqual(len(self.webhook.received), 1)

    def test_generated_day_delivers_every_message_in_order(self):
        scenario = generate(42, "2026-11-07")
        path = Path(self.temp.name) / "generated.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"], timeout=10)
        self.assertEqual(state["status"], "completed", state["error"])
        self.assertEqual(state["delivered"], len(scenario["events"]))
        self.assertEqual([r[0] for r in self.webhook.received], [e["message"] for e in scenario["events"]])

    def test_stop_wakes_paced_wait(self):
        self.options.update(mode="paced", speed=1)
        _, created = self.request("POST", "/runs", self.options)
        self.assertTrue(self.webhook.entered.wait(2))
        self.assertEqual(self.request("POST", f"/runs/{created['run_id']}/stop")[0], 202)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("stopped", 1))

    def test_concurrent_starts_have_one_winner(self):
        self.webhook.release.clear()
        barrier = threading.Barrier(3)
        codes = []

        def start():
            barrier.wait()
            codes.append(self.request("POST", "/runs", self.options)[0])

        workers = [threading.Thread(target=start) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(codes), [201, 409])

    def test_webhook_http_error_ends_run(self):
        self.webhook.response_status = 503
        _, created = self.request("POST", "/runs", self.options)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["attempted"], state["delivered"]), ("failed", 1, 0))
        self.assertIn("503", state["error"])
        self.assertEqual(len(self.webhook.received), 1)

    def test_bad_requests_and_invalid_fixture_deliver_nothing(self):
        for body in ([], {}, {**self.options, "speed": False}, {**self.options, "speed": 10**400},
                     {**self.options, "extra": 1}, {**self.options, "scenario_path": None},
                     {**self.options, "scenario_path": "/nonexistent-scenario.json"},
                     {**self.options, "mode": {}}, {**self.options, "webhook_url": []}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/runs", body)[0], 400)
        for raw in ("{", "null", '{"speed": NaN}'):
            self.assertEqual(self.request("POST", "/runs", raw=raw)[0], 400)
        self.assertEqual(self.request("POST", "/runs", self.options, headers={})[0], 400)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)
        self.assertEqual(self.request("GET", "/runs/missing")[0], 404)
        self.assertEqual(self.request("POST", "/runs/missing/stop")[0], 404)
        invalid = load_scenario(FIXTURE)
        invalid["events"][-1]["message"]["legs"][0]["scheduledDepartureTime"] = "25:00"
        path = Path(self.temp.name) / "invalid.json"
        path.write_text(json.dumps(invalid))
        self.options["scenario_path"] = str(path)
        self.assertEqual(self.request("POST", "/runs", self.options)[0], 400)
        self.assertEqual(self.webhook.received, [])
        self.assertEqual(self.control.manager.runs, {})


if __name__ == "__main__":
    unittest.main()
