"""Sequential webhook delivery on an anchored simulation clock."""

import copy
import http.client
import json
import math
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .gates import GateState
from .validation import DEFAULT_RATE, load_scenario, parse_utc, positive_number, read_json, require


WEBHOOK_TIMEOUT = 10
MAX_RESPONSE_BYTES = 1024 * 1024
ACTIVE_STATUSES = {"running", "stopping"}
_USE_SCENARIO_RATE = object()


def validate_webhook(url):
    require(isinstance(url, str) and not any(ord(c) <= 32 for c in url),
            "webhook_url must be an HTTP(S) URL")
    parts = urlsplit(url)
    require(parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None and not parts.fragment,
            "webhook_url must be HTTP(S), without credentials or a fragment")
    require(parts.port is None or 1 <= parts.port <= 65535, "invalid webhook port")
    return parts


def post_message(url, message, headers):
    parts = validate_webhook(url)
    connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parts.hostname, parts.port, timeout=WEBHOOK_TIMEOUT)
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request("POST", target, json.dumps(message, allow_nan=False).encode("utf-8"),
                           {"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            return response.status, None
        data = response.read(MAX_RESPONSE_BYTES + 1)
        require(len(data) <= MAX_RESPONSE_BYTES, "matcher response exceeds 1 MiB")
        if not data:
            return response.status, None
        require(response.status == 200, "matcher decision bodies require HTTP 200")
        require(response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json",
                "matcher decisions require Content-Type application/json")
        return response.status, read_json(data)
    finally:
        connection.close()


class Run:
    def __init__(self, scenario_path, webhook_url, mode="paced", speed=_USE_SCENARIO_RATE):
        require(mode in ("paced", "unpaced"), "mode must be paced or unpaced")
        validate_webhook(webhook_url)
        self.scenario = load_scenario(scenario_path)
        if speed is _USE_SCENARIO_RATE:
            speed = self.scenario.get("metadata", {}).get("rate", DEFAULT_RATE)
        speed = positive_number(speed, "speed")
        timestamps = [parse_utc(e["timestamp"]) for e in self.scenario["events"]]
        self.offsets = [(timestamp - timestamps[0]).total_seconds() / speed for timestamp in timestamps]
        require(all(math.isfinite(offset) for offset in self.offsets), "speed produces infinite deadlines")
        self.gate_state = GateState()
        first = self.scenario["events"][0]
        if first["message"]["action"] == "InitializeAirport":
            self.gate_state.apply_message(first["message"], first["timestamp"])
            self.gate_state.check_baseline_capacity()
        self.id = uuid.uuid4().hex
        self.webhook_url = webhook_url
        self.mode = mode
        self.speed = speed
        self.directory = Path(tempfile.mkdtemp(prefix="smartgate-stream-"))
        (self.directory / "scenario.json").write_text(
            json.dumps(self.scenario, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        self._save_gates()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.events_condition = threading.Condition(self.lock)
        self.decision_events = []
        self.latest_decision = None
        self.thread = None
        self.clock = time.monotonic
        self.started = None
        self.finished = None
        self.state = {
            "run_id": self.id, "status": "running", "mode": mode, "speed": speed,
            "total_events": len(timestamps), "attempted": 0, "delivered": 0,
            "simulated_time": None, "http_seconds": 0, "lateness_seconds": 0,
            "max_lateness_seconds": 0, "failed_event_id": None, "error": None,
            "records_directory": str(self.directory),
        }

    def snapshot(self):
        with self.lock:
            elapsed = 0 if self.started is None else (self.finished if self.finished is not None else self.clock()) - self.started
            return {
                **self.state,
                "elapsed_seconds": elapsed,
                "decision_event_count": len(self.decision_events),
                "latest_decision": copy.deepcopy(self.latest_decision),
                **self.gate_state.snapshot(),
            }

    def publish_decision_event(self, event):
        """Publish one completed event result for dashboard/API consumers."""
        with self.events_condition:
            event = copy.deepcopy(event)
            event["sequence"] = len(self.decision_events) + 1
            self.decision_events.append(event)
            self.latest_decision = event
            self.events_condition.notify_all()
            return copy.deepcopy(event)

    def events_after(self, sequence=0):
        with self.lock:
            return copy.deepcopy([
                event for event in self.decision_events
                if event["sequence"] > sequence
            ])

    def wait_for_events(self, sequence=0, timeout=15):
        with self.events_condition:
            if not any(event["sequence"] > sequence for event in self.decision_events):
                if self.state["status"] in ACTIVE_STATUSES:
                    self.events_condition.wait(timeout)
            return copy.deepcopy([
                event for event in self.decision_events
                if event["sequence"] > sequence
            ])

    def _save_gates(self):
        temporary = self.directory / ".gate-state.json.tmp"
        temporary.write_text(json.dumps(self.gate_state.gates, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "gate-state.json")

    def stop(self):
        with self.lock:
            if self.state["status"] not in ACTIVE_STATUSES:
                return False
            self.state["status"] = "stopping"
            self.stop_event.set()
            self.events_condition.notify_all()
            return True

    def start(self):
        self.thread = threading.Thread(target=self.execute, name=f"stream-{self.id}", daemon=True)
        self.thread.start()

    def execute(self, *, clock=time.monotonic, wait=None, send=post_message):
        self.clock = clock
        wait = wait or self.stop_event.wait
        self.started = clock()
        event_id = None
        initialization_seconds = 0
        first_time = parse_utc(self.scenario["events"][0]["timestamp"])

        def wait_until(deadline):
            if deadline is not None:
                while not self.stop_event.is_set():
                    delay = self.started + deadline - clock()
                    if delay <= 0:
                        break
                    wait(min(delay, threading.TIMEOUT_MAX))
            return not self.stop_event.is_set()

        def advance_movements(until=None):
            while (when := self.gate_state.next_movement()) is not None and (until is None or when <= until):
                deadline = initialization_seconds + (when - first_time).total_seconds() / self.speed if self.mode == "paced" else None
                if not wait_until(deadline):
                    return False
                with self.lock:
                    if self.stop_event.is_set():
                        return False
                    self.gate_state.advance(when)
                    self._save_gates()
            return True

        try:
            with (self.directory / "deliveries.jsonl").open("a", encoding="utf-8") as records:
                for index, event in enumerate(self.scenario["events"]):
                    event_id = f"{self.id}:{index}"
                    deadline = initialization_seconds + self.offsets[index] if self.mode == "paced" else None
                    timestamp = parse_utc(event["timestamp"])
                    if not advance_movements(timestamp) or not wait_until(deadline):
                        break
                    with self.lock:
                        if self.stop_event.is_set():
                            break
                        if event["message"]["action"] != "InitializeAirport":
                            self.gate_state.apply_message(event["message"], timestamp)
                        payload = self.gate_state.request(event["message"])
                        gate_state_before_decision = copy.deepcopy(self.gate_state.gates)
                        self._save_gates()
                        sent = clock()
                        self.state["attempted"] += 1
                        self.state["simulated_time"] = event["timestamp"]
                    lateness = 0 if deadline is None else max(0, sent - self.started - deadline)
                    record = {
                        "event_id": event_id, "simulated_time": event["timestamp"],
                        "intended_deadline_seconds": deadline,
                        "actual_send_seconds": sent - self.started,
                        "actual_send_time": datetime.now(timezone.utc).isoformat(),
                        "lateness_seconds": lateness, "http_status": None, "error": None, "matcher_response": None,
                    }
                    received_at = None
                    response = None
                    try:
                        record["http_status"], response = send(self.webhook_url, payload, {
                            "X-Stream-Run-Id": self.id,
                            "X-Stream-Event-Id": event_id,
                            "X-Stream-Simulated-Time": event["timestamp"],
                        })
                        received_at = clock()
                        record["matcher_response"] = response
                        if not 200 <= record["http_status"] < 300:
                            record["error"] = f"webhook returned HTTP {record['http_status']}"
                        elif event["message"]["action"] == "InitializeAirport" and record["http_status"] not in (200, 204):
                            record["error"] = "InitializeAirport requires HTTP 200 or 204 after initial allocation is ready"
                        if record["error"] is None:
                            with self.lock:
                                self.gate_state.apply_decisions(response, initializing=event["message"]["action"] == "InitializeAirport")
                                self._save_gates()
                    except Exception as error:
                        record["error"] = f"{type(error).__name__}: {error}"
                    record["http_seconds"] = (received_at if received_at is not None else clock()) - sent
                    with self.lock:
                        self.state["http_seconds"] += record["http_seconds"]
                        self.state["lateness_seconds"] += lateness
                        self.state["max_lateness_seconds"] = max(self.state["max_lateness_seconds"], lateness)
                        if record["error"] is None:
                            self.state["delivered"] += 1

                    after_state = self.gate_state.snapshot()
                    response_object = response if isinstance(response, dict) else {}
                    before_gates = gate_state_before_decision
                    after_gates = after_state["gate_state"]
                    gate_changes = [
                        {
                            "gate": gate,
                            "before": before_gates.get(gate),
                            "after": after_gates.get(gate),
                        }
                        for gate in after_gates
                        if before_gates.get(gate) != after_gates.get(gate)
                    ]
                    self.publish_decision_event({
                        "event_id": event_id,
                        "event_type": event["message"]["action"],
                        "event_timestamp": event["timestamp"],
                        # Simulation time is paused while the synchronous matcher
                        # handles this event, so its decision belongs to this instant.
                        "decision_made_at": event["timestamp"],
                        "decision_received_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "decision_duration_seconds": record["http_seconds"],
                        "http_status": record["http_status"],
                        "decision_status": "applied" if record["error"] is None else "failed",
                        "error": record["error"],
                        "assignments": copy.deepcopy(response_object.get("assignments", [])),
                        "moves": copy.deepcopy(response_object.get("moves", [])),
                        "gate_changes": gate_changes,
                        "gate_state": after_gates,
                        "gate_state_time": after_state["gate_state_time"],
                        "unassigned_aircraft": after_state["unassigned_aircraft"],
                        "planned_assignments": after_state["planned_assignments"],
                    })
                    records.write(json.dumps(record, allow_nan=False) + "\n")
                    records.flush()
                    if record["error"]:
                        raise RuntimeError(record["error"])
                    if event["message"]["action"] == "InitializeAirport":
                        initialization_seconds = clock() - self.started
                if self.gate_state.initialized and not self.stop_event.is_set():
                    advance_movements()
            with self.lock:
                self.state["status"] = "stopped" if self.stop_event.is_set() else "completed"
                self.finished = clock()
                self.events_condition.notify_all()
        except Exception as error:
            with self.events_condition:
                self.state.update(status="failed", failed_event_id=event_id,
                                  error=f"{type(error).__name__}: {error}")
                self.finished = clock()
                self.events_condition.notify_all()
