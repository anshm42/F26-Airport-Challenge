import http.client
import copy
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


def message_fields(message):
    return {key: value for key, value in message.items() if key not in ("gateState", "unassignedAircraft")}


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
        status = (self.server.initialization_status if message["action"] == "InitializeAirport"
                  else self.server.response_status)
        body = self.server.response_body
        if callable(body):
            body = body(message)
        encoded = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if encoded:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)


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
        self.webhook.initialization_status = 204
        self.webhook.response_body = None
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
        expected = load_scenario(FIXTURE)["events"]
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("completed", len(expected)))
        self.assertEqual([message_fields(r[0]) for r in self.webhook.received], [e["message"] for e in expected])
        for index, (_, headers) in enumerate(self.webhook.received):
            headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(headers["x-stream-event-id"], f"{state['run_id']}:{index}")
            self.assertEqual(headers["x-stream-simulated-time"], expected[index]["timestamp"])
        records = Path(state["records_directory"]) / "deliveries.jsonl"
        self.assertEqual(len(records.read_text().splitlines()), len(expected))
        self.assertEqual(self.request("POST", f"/runs/{state['run_id']}/stop")[0], 409)
        code, second = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        self.assertNotEqual(second["run_id"], state["run_id"])
        self.terminal(second["run_id"])
        self.assertEqual(self.request("GET", f"/runs/{state['run_id']}")[1]["delivered"], len(expected))

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
        initial = scenario["events"][0]["message"]
        self.assertEqual(initial["action"], "InitializeAirport")
        self.assertEqual(len(initial["flights"]), 1200)
        self.assertTrue(initial["parkedAircraft"])
        early_updates = [(index, event) for index, event in enumerate(scenario["events"])
                         if event["message"]["action"] == "UpdateTiming"
                         and event["message"]["legs"][0]["arrivalStation"] == "YYZ"]
        self.assertTrue(early_updates, "generated day must exercise early inbound arrivals")
        path = Path(self.temp.name) / "generated.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"], timeout=10)
        self.assertEqual(state["status"], "completed", state["error"])
        self.assertEqual(state["delivered"], len(scenario["events"]))
        self.assertEqual([message_fields(r[0]) for r in self.webhook.received],
                         [message_fields(e["message"]) for e in scenario["events"]])
        for index, event in early_updates:
            headers = {k.lower(): v for k, v in self.webhook.received[index][1].items()}
            self.assertEqual(headers["x-stream-event-id"], f"{state['run_id']}:{index}")
            self.assertEqual(headers["x-stream-simulated-time"], event["timestamp"])

    def test_generated_initialization_blocks_updates_until_ready(self):
        scenario = generate(42, "2026-11-07", 24)
        path = Path(self.temp.name) / "initialized.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        self.webhook.release.clear()
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        self.assertTrue(self.webhook.entered.wait(2))
        self.assertEqual([m["action"] for m, _ in self.webhook.received], ["InitializeAirport"])
        self.assertEqual(self.request("GET", f"/runs/{created['run_id']}")[1]["delivered"], 0)
        self.webhook.release.set()
        self.assertEqual(self.terminal(created["run_id"])["status"], "completed")
        self.assertEqual([message_fields(m) for m, _ in self.webhook.received],
                         [message_fields(e["message"]) for e in scenario["events"]])

    def test_generated_initialization_rejection_sends_no_updates(self):
        scenario = generate(42, "2026-11-07", 24)
        path = Path(self.temp.name) / "rejected.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        for status in (202, 422):
            with self.subTest(status=status):
                self.webhook.initialization_status = status
                self.webhook.received.clear()
                code, created = self.request("POST", "/runs", self.options)
                self.assertEqual(code, 201)
                state = self.terminal(created["run_id"])
                self.assertEqual((state["status"], state["attempted"], state["delivered"]), ("failed", 1, 0))
                self.assertEqual([m["action"] for m, _ in self.webhook.received], ["InitializeAirport"])

    def gate_scenario(self):
        events = load_scenario(FIXTURE)["events"]
        departure = copy.deepcopy(events[0]["message"])
        arrival = copy.deepcopy(events[7]["message"])
        arrival["action"] = "CreateFlight"
        arrival.pop("emergencyState")
        arrival.pop("reason")
        initial = {"action": "InitializeAirport", "flights": [departure, arrival],
                   "parkedAircraft": [{**departure["flight"], "gate": "D20"}], "gateOutages": []}
        scenario = {"events": [
            {"timestamp": "2026-11-07T05:00:00Z", "message": initial},
            {"timestamp": "2026-11-07T12:30:00Z", "message": {
                "action": "GateOutage", "gate": "Gate 132", "scheduledStartTime": "08:00", "scheduledEndTime": "10:00"}},
            {"timestamp": "2026-11-07T13:05:00Z", "message": {
                "action": "GateOutage", "gate": "Gate 124", "scheduledStartTime": "09:00", "scheduledEndTime": "10:00"}},
        ]}
        path = Path(self.temp.name) / "gates.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        self.webhook.initialization_status = self.webhook.response_status = 200
        return initial, arrival

    def test_matcher_choices_wait_for_arrival_and_current_state_is_sent_next(self):
        initial, arrival = self.gate_scenario()
        leg = arrival["legs"][0]
        choice = {"carrier": arrival["flight"]["carrier"], "leg": {key: leg.get(key) for key in (
            "flightNumber", "flightSuffix", "originatingDate", "departureStation", "arrivalStation")}, "gate": "D41"}
        self.webhook.response_body = lambda message: {
            "assignments": [choice] if message["action"] == "InitializeAirport" else []}
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("completed", 3))
        received = [message for message, _ in self.webhook.received]
        self.assertTrue(all(len(message["gateState"]) == 110 for message in received))
        self.assertEqual(received[0]["gateState"]["D20"], initial["flights"][0]["flight"])
        self.assertIsNone(received[0]["gateState"]["D41"])
        self.assertIsNone(received[1]["gateState"]["D41"])
        self.assertEqual(received[2]["gateState"]["D41"], arrival["flight"])
        self.assertIsNone(state["gate_state"]["D20"])
        self.assertEqual(state["gate_state"]["D41"], arrival["flight"])
        self.assertEqual(state["gate_state_time"], "2026-11-07T14:00:00Z")
        saved = Path(state["records_directory"]) / "gate-state.json"
        self.assertEqual(json.loads(saved.read_text()), state["gate_state"])
        records = Path(state["records_directory"]) / "deliveries.jsonl"
        self.assertEqual(json.loads(records.read_text().splitlines()[0])["matcher_response"], {"assignments": [choice]})

    def test_overcapacity_baseline_is_rejected_before_any_delivery(self):
        initial, arrival = self.gate_scenario()
        initial["parkedAircraft"] = []
        initial["flights"] = []
        for index in range(4):
            incoming = copy.deepcopy(arrival)
            incoming["flight"].update(registration=f"C-OVER{index}", aircraftType="388")
            incoming["legs"][0]["flightNumber"] = str(900 + index)
            initial["flights"].append(incoming)
        path = Path(self.options["scenario_path"])
        scenario = json.loads(path.read_text())
        scenario["events"][0]["message"] = initial
        path.write_text(json.dumps(scenario))
        status, error = self.request("POST", "/runs", self.options)
        self.assertEqual(status, 400)
        self.assertIn("baseline schedule exceeds passenger gate capacity", error["error"])
        self.assertEqual(self.webhook.received, [])
        self.assertEqual(self.control.manager.runs, {})

    def test_invalid_matcher_batch_keeps_occupancy_and_stops_delivery(self):
        initial, _ = self.gate_scenario()
        tail = initial["parkedAircraft"][0]["registration"]
        invalid = {"moves": [{"registration": tail, "gate": "D22"},
                              {"registration": "C-UNKNOWN", "gate": "D24"}]}
        self.webhook.response_body = invalid
        code, created = self.request("POST", "/runs", self.options)
        self.assertEqual(code, 201)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["attempted"], state["delivered"]), ("failed", 1, 0))
        self.assertEqual(len(self.webhook.received), 1)
        self.assertEqual(state["gate_state"]["D20"], initial["flights"][0]["flight"])
        self.assertIsNone(state["gate_state"]["D22"])
        records = Path(state["records_directory"]) / "deliveries.jsonl"
        record = json.loads(records.read_text().splitlines()[0])
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["matcher_response"], invalid)

    def test_stop_wakes_paced_wait(self):
        self.options.update(mode="paced", speed=1)
        _, created = self.request("POST", "/runs", self.options)
        self.assertTrue(self.webhook.entered.wait(2))
        self.assertEqual(self.request("POST", f"/runs/{created['run_id']}/stop")[0], 202)
        state = self.terminal(created["run_id"])
        self.assertEqual((state["status"], state["delivered"]), ("stopped", 1))

    def test_generated_rate_is_used_unless_request_overrides_speed(self):
        scenario = generate(42, "2026-11-07", 2, rate=0.5)
        path = Path(self.temp.name) / "rated.json"
        path.write_text(json.dumps(scenario))
        self.options["scenario_path"] = str(path)
        for override, expected in (({}, 0.5), ({"speed": 120}, 120)):
            with self.subTest(override=override):
                code, created = self.request("POST", "/runs", {**self.options, **override})
                self.assertEqual(code, 201)
                state = self.terminal(created["run_id"])
                self.assertEqual(state["speed"], expected)
        scenario["metadata"]["rate"] = 0
        path.write_text(json.dumps(scenario))
        before = len(self.webhook.received)
        self.assertEqual(self.request("POST", "/runs", self.options)[0], 400)
        self.assertEqual(len(self.webhook.received), before)

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
