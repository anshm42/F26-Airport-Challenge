import base64
import contextlib
import http.client
import io
import json
import os
import socket
import struct
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import matcher_server as matcher
from matcher_views import SimClock, ViewFeed
from smartgate_stream.gates import GateState
from smartgate_stream.inventory import initial_gate_state
from test_stream_gates import INBOUND, PARKED, flight

RUN = "test-run"
DEFAULTS = {
    "STATE": None, "ASSIGNMENTS": {}, "GATE_STARTS": {}, "CURRENT_RUN_ID": None,
    "LAST_EVENT_INDEX": -1, "LAST_EVENT_ID": None, "LATEST_GATE_STATE": {},
    "PREDICTED_GATE_STATE": {}, "PREDICTED_UNASSIGNED_AIRCRAFT": [], "LATEST_UNASSIGNED_AIRCRAFT": [],
    "LATEST_SIMULATED_TIME": None, "LAST_SOLVER_SUMMARY": {},
    "LAST_DECISION_RESPONSE": {"assignments": [], "moves": []}, "ASSIGNED_AT": {},
    "VISIT_POSITIONS": {}, "UNASSIGNED_REASONS": {}, "DEFERRED_SESSIONS": {}, "MISSED_EVENTS": [],
}


def stand_in_solver(backend, payload):
    """Julia's line protocol answered by the Python allocator; tests don't need Gurobi."""
    sessions = {session["id"]: session for session in payload["sessions"]}
    result = matcher.repair_assignments_python(sessions, payload["gates"], payload["outages"], {}, set(sessions))
    return {"assignments": result["assignments"], "unassigned": result["unassigned"]}


def leg_numbers(response):
    return sorted(decision["leg"]["flightNumber"] for decision in response["assignments"])


class MatcherServerTests(unittest.TestCase):
    def setUp(self):
        saved = {name: getattr(matcher, name) for name in (*DEFAULTS, "VIEW_CLOCK", "VIEW_FEED")}
        self.addCleanup(lambda: [setattr(matcher, name, value) for name, value in saved.items()])
        for name, value in DEFAULTS.items():
            setattr(matcher, name, value)
        matcher.RESPONSE_CACHE.clear()
        matcher.VIEW_CLOCK = SimClock()
        matcher.VIEW_FEED = ViewFeed(matcher.capture_view_snapshot, matcher.VIEW_CLOCK)
        self.julia = patch.object(matcher, "run_julia", side_effect=stand_in_solver).start()
        self.addCleanup(patch.stopall)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), matcher.MatcherHandler)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(2)))

        self.stream = GateState()
        departure = flight("100", "YYZ", "YUL", "06:00", "07:15", PARKED)
        inbound = flight("200", "YUL", "YYZ", "07:00", "08:00")
        outbound = flight("201", "YYZ", "YUL", "09:00", "10:15")
        parked, gates = initial_gate_state([{**PARKED, "gate": "D20"}])
        initial = {"action": "InitializeAirport", "flights": [departure, inbound, outbound],
                   "parkedAircraft": parked, "gateOutages": [], "gateState": gates}
        self.stream.apply_message(initial, "2026-11-07T05:00:00Z")
        self.initial = self.stream.request(initial)

    def outage(self, gate="Gate 140", start="12:00", end="13:00"):
        return {"action": "GateOutage", "gate": gate, "scheduledStartTime": start, "scheduledEndTime": end,
                **{key: value for key, value in self.stream.request({}).items()}}

    def post(self, body, index, timestamp="2026-11-07T05:00:00Z", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                connection.request("POST", "/updates", json.dumps(body), {
                    "Content-Type": "application/json", "X-Stream-Run-Id": RUN,
                    "X-Stream-Event-Id": f"{RUN}:{index}", "X-Stream-Simulated-Time": timestamp,
                    **(headers or {})})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    def test_julia_failure_commits_nothing_and_initialization_can_be_retried(self):
        self.julia.side_effect = RuntimeError("Julia solver is not running; a restart is in progress.")
        self.assertEqual(self.post(self.initial, 0)[0], 500)
        self.assertIsNone(matcher.STATE)
        self.julia.side_effect = stand_in_solver
        status, response = self.post(self.initial, 0)
        self.assertEqual(status, 200)
        self.assertEqual(leg_numbers(response), ["100", "200", "201"])
        self.assertEqual(self.julia.call_args.args[1]["timeLimit"], matcher.GUROBI_TIME_LIMIT)

    def test_retried_event_gets_the_committed_response_without_reapplying(self):
        _, initialized = self.post(self.initial, 0)
        self.assertEqual(self.post(self.initial, 0), (200, initialized))
        with patch.object(matcher, "_handle_update", wraps=matcher._handle_update) as update:
            first = self.post(self.outage(), 1, "2026-11-07T06:00:00Z")
            retry = self.post(self.outage(), 1, "2026-11-07T06:00:00Z", {"X-Stream-Attempt": "2"})
        self.assertEqual(first[0], 200)
        self.assertEqual(retry, first)
        self.assertEqual(update.call_count, 1)
        self.assertEqual(len(matcher.STATE.outages), 1)

    def test_skipped_events_are_accepted_and_the_full_plan_is_resent(self):
        self.post(self.initial, 0)
        status, response = self.post(self.outage(), 3, "2026-11-07T06:00:00Z")
        self.assertEqual(status, 200)
        # An unrelated outage changes no plan, but after a gap the whole plan is resent.
        self.assertEqual(leg_numbers(response), ["100", "200", "201"])
        self.assertEqual(self.get("/health")["missed_events"], [[1, 2]])
        self.assertEqual(self.post(self.outage(), 2, "2026-11-07T06:00:00Z")[0], 409)
        status, response = self.post(self.outage(), 4, "2026-11-07T06:01:00Z")
        self.assertEqual((status, response), (200, {"assignments": [], "moves": []}))
        status, response = self.post(self.outage(), 5, "2026-11-07T06:02:00Z", {"X-Stream-Resync": "true"})
        self.assertEqual(leg_numbers(response), ["100", "200", "201"])

    def test_failed_event_commits_nothing_and_can_be_retried(self):
        self.post(self.initial, 0)
        state = matcher.STATE
        with patch.object(matcher, "build_stream_decisions", side_effect=RuntimeError("decision bug")):
            status, response = self.post(self.outage(), 1, "2026-11-07T06:00:00Z")
        self.assertEqual((status, response), (500, {"error": "decision bug"}))
        self.assertIs(matcher.STATE, state)
        self.assertEqual((matcher.LAST_EVENT_INDEX, matcher.LATEST_SIMULATED_TIME), (0, "2026-11-07T05:00:00Z"))
        self.assertEqual(self.post(self.outage(), 1, "2026-11-07T06:00:00Z")[0], 200)
        self.assertEqual((matcher.LAST_EVENT_INDEX, len(matcher.STATE.outages)), (1, 1))

    def test_updates_before_initialization_or_from_another_run_are_refused(self):
        self.assertEqual(self.post(self.outage(), 1)[0], 409)
        self.post(self.initial, 0)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        with contextlib.redirect_stderr(io.StringIO()):
            connection.request("POST", "/updates", json.dumps(self.outage()), {
                "Content-Type": "application/json", "X-Stream-Run-Id": "other",
                "X-Stream-Event-Id": "other:1", "X-Stream-Simulated-Time": "2026-11-07T06:00:00Z"})
            self.assertEqual(connection.getresponse().status, 409)
        connection.close()

    def test_physically_blocked_tow_is_deferred_instead_of_failing(self):
        self.post(self.initial, 0)
        sessions = matcher.STATE.build_sessions()
        inbound = next(sid for sid, session in sessions.items() if session["registration"] == INBOUND["registration"])
        blocker = {"carrier": "AC", "registration": "C-BLOCK", "aircraftType": "321"}
        matcher.LATEST_SIMULATED_TIME = "2026-11-07T13:30:00Z"  # 08:30, the inbound is on the ground
        matcher.LATEST_UNASSIGNED_AIRCRAFT = [INBOUND]
        matcher.LATEST_GATE_STATE = {**self.initial["gateState"], "D20": None, "D24": blocker}
        assignments = {inbound: "D24"}
        sendable, deferred = matcher.defer_blocked_current_visits(
            matcher.STATE, sessions, assignments, {}, {inbound})
        self.assertEqual(sendable, set())
        self.assertIn("physically occupied by C-BLOCK", deferred[inbound])
        self.assertEqual(matcher.build_stream_decisions(matcher.STATE, sessions, assignments, {}, sendable),
                         {"assignments": [], "moves": []})
        matcher.LATEST_GATE_STATE = {**matcher.LATEST_GATE_STATE, "D24": None}
        sendable, deferred = matcher.defer_blocked_current_visits(
            matcher.STATE, sessions, assignments, {}, {inbound})
        self.assertEqual((sendable, deferred), ({inbound}, {}))

    def websocket(self, path):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        self.addCleanup(sock.close)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reader = sock.makefile("rb")
        self.assertIn(b" 101 ", reader.readline())
        while reader.readline() not in (b"\r\n", b""):
            pass

        def receive():
            head = reader.read(2)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", reader.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", reader.read(8))[0]
            return json.loads(reader.read(length))
        return receive

    def test_view_websockets_send_full_state_then_heartbeats(self):
        receive = self.websocket("/operator/ws")
        empty = receive()
        self.assertEqual((empty["type"], empty["flights"]), ("assignments", []))

        self.post(self.initial, 0)
        passenger = self.websocket("/passenger/ws")
        state = passenger()
        self.assertEqual(state["type"], "assignments")
        self.assertEqual(state["generatedAt"], "2026-11-07T00:00:00-05:00")
        flights = {item["flightCodes"][0]: item for item in state["flights"]}
        self.assertEqual(set(flights), {"AC100", "AC200", "AC201"})
        inbound, outbound = flights["AC200"], flights["AC201"]
        self.assertEqual(inbound["linkedFlightId"], outbound["flightId"])
        self.assertEqual(inbound["gate"], outbound["gate"])
        self.assertEqual(inbound["timeArrival"], "2026-11-07T08:00:00-05:00")
        self.assertEqual(inbound["occupancy"], {"start": "2026-11-07T08:00:00-05:00",
                                                "end": "2026-11-07T09:00:00-05:00"})
        self.assertEqual(inbound["previous"], {})
        self.assertEqual(flights["AC100"]["gate"], {"terminal": "1", "gate": "D20"})

        # The operator client connected first: the publish brings it the same state.
        self.assertEqual(receive()["flights"], state["flights"])
        matcher.VIEW_FEED.publish()
        self.assertEqual(receive()["type"], "heartbeat")
        self.assertEqual(passenger()["type"], "heartbeat")

        with patch.object(matcher, "repair_assignments_python",
                          return_value={"assignments": [], "unassigned": [], "unassigned_reasons": {}}):
            self.post(self.outage(), 1, "2026-11-07T06:00:00Z")
            self.post({"action": "CancelFlight", "flight": PARKED, "legs": [flight("100", "YYZ", "YUL", "06:00",
                       "07:15", PARKED)["legs"][0]], "cancelScope": {"type": "AllLegs"},
                       **self.stream.request({})}, 2, "2026-11-07T06:01:00Z")
        matcher.VIEW_FEED.publish()
        update = receive()
        cancelled = next(item for item in update["flights"] if item["flightCodes"] == ["AC100"])
        self.assertTrue(cancelled["cancelled"])
        self.assertIsNone(cancelled["gate"])
        self.assertEqual(cancelled["previous"]["gate"], {"terminal": "1", "gate": "D20"})
        self.assertEqual(update["outages"], [{"stand": "Gate 140", "start": "2026-11-07T12:00:00-05:00",
                                              "end": "2026-11-07T13:00:00-05:00", "misc": {}}])


if __name__ == "__main__":
    unittest.main()
