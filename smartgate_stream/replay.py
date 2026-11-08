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

from .gates import GateState, utc_string
from .validation import DEFAULT_RATE, flight_key, load_scenario, parse_utc, positive_number, read_json, require


WEBHOOK_TIMEOUT = 10
MAX_RESPONSE_BYTES = 1024 * 1024
ACTIVE_STATUSES = {"running", "stopping"}
_USE_SCENARIO_RATE = object()

# Transport failures and 5xx are retried under the same X-Stream-Event-Id, which the
# matcher uses as an idempotency key; other failures would only repeat.
RETRYABLE_ERRORS = (OSError, http.client.HTTPException)
UPDATE_ATTEMPTS = 3
# The matcher may still be starting its solver; keep offering the initialization.
INITIALIZE_RETRY_SECONDS = 180
RETRY_BACKOFF_SECONDS = (0.5, 1, 2, 5)


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


def apply_valid_decisions(gate_state, response, *, initializing=False):
    """Apply the valid part of a matcher response; return the rejected groups instead of raising.

    GateState.apply_decisions is atomic. The whole response is tried first. If it is
    invalid, each aircraft's leg assignments and move form their own atomic group, so
    one infeasible aircraft cannot block the others. Failed groups are retried once the
    rest are applied, since a tow into a gate can depend on another tow out of it.
    """
    try:
        gate_state.apply_decisions(response, initializing=initializing)
        return []
    except Exception as error:
        whole_error = str(error)
    if not (isinstance(response, dict) and set(response) <= {"assignments", "moves"}
            and isinstance(response.get("assignments", []), list)
            and isinstance(response.get("moves", []), list)):
        return [{"assignments": [], "moves": [], "error": whole_error}]
    groups = {}
    for decision in response.get("assignments", []):
        try:
            tail = gate_state.flights[flight_key(decision["carrier"], decision["leg"])]["aircraft"]["registration"]
        except Exception:
            tail = ("unknown", len(groups))
        groups.setdefault(tail, {"assignments": [], "moves": []})["assignments"].append(decision)
    for move in response.get("moves", []):
        tail = move.get("registration") if isinstance(move, dict) else None
        if not isinstance(tail, str):
            tail = ("unknown", len(groups))
        groups.setdefault(tail, {"assignments": [], "moves": []})["moves"].append(move)

    def salvage(pending):
        # Bisect so a few bad groups cost O(bad * log n) state copies, not one per group.
        if not pending:
            return []
        try:
            gate_state.apply_decisions({"assignments": [d for g in pending for d in g["assignments"]],
                                        "moves": [m for g in pending for m in g["moves"]]},
                                       initializing=initializing)
            return []
        except Exception as error:
            if len(pending) == 1:
                return [(pending[0], str(error))]
        middle = len(pending) // 2
        return salvage(pending[:middle]) + salvage(pending[middle:])

    rejected = salvage(list(groups.values()))
    while rejected:
        retried = salvage([group for group, _ in rejected])
        if len(retried) == len(rejected):
            break
        rejected = retried
    return [{**group, "error": error} for group, error in rejected]


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
        self.decision_events_path = self.directory / "decision-events.jsonl"
        self.decision_events_path.write_text("", encoding="utf-8")
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
        self.update_attempts = UPDATE_ATTEMPTS
        self.initialize_retry_seconds = INITIALIZE_RETRY_SECONDS
        # Set when an event's decisions were never applied, so the next request asks
        # the matcher for its full plan instead of only what changed.
        self.resync_pending = False
        self.reassessment_enabled = False
        self.reassessment_at = None
        self.state = {
            "run_id": self.id, "status": "running", "mode": mode, "speed": speed,
            "total_events": len(timestamps), "attempted": 0, "delivered": 0,
            "reassessments_attempted": 0, "reassessments_delivered": 0,
            "retries": 0, "skipped": 0, "skipped_event_ids": [], "rejected_decisions": 0,
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
            with self.decision_events_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
                output.flush()
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

    def _deliver(self, send, payload, headers, *, initializing, clock, wait):
        """Send one event, retrying transport failures under the same event ID.

        A response is applied only from the attempt that returns it, so the stream
        applies each event's decisions at most once even when the matcher saw it twice.
        Returns (status, response, attempts, error).
        """
        attempts = []
        first_sent = clock()
        while True:
            attempt_headers = dict(headers)
            if attempts:
                attempt_headers["X-Stream-Attempt"] = str(len(attempts) + 1)
            sent = clock()
            status, response, error, retryable = None, None, None, False
            try:
                status, response = send(self.webhook_url, payload, attempt_headers)
                if not 200 <= status < 300:
                    error, retryable = f"webhook returned HTTP {status}", status >= 500
                elif initializing and status not in (200, 204):
                    error = "InitializeAirport requires HTTP 200 or 204 after initial allocation is ready"
            except RETRYABLE_ERRORS as exc:
                error, retryable = f"{type(exc).__name__}: {exc}", True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            attempts.append({"http_status": status, "error": error, "seconds": clock() - sent})
            if error is None:
                return status, response, attempts, None
            exhausted = (clock() - first_sent >= self.initialize_retry_seconds if initializing
                         else len(attempts) >= self.update_attempts)
            if not retryable or exhausted or self.stop_event.is_set():
                return status, None, attempts, error
            wait(RETRY_BACKOFF_SECONDS[min(len(attempts), len(RETRY_BACKOFF_SECONDS)) - 1])
            if self.stop_event.is_set():
                return status, None, attempts, error
            with self.lock:
                self.state["retries"] += 1

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

        def scheduled_events():
            index = 0
            events = self.scenario["events"]
            while not self.stop_event.is_set():
                source = events[index] if index < len(events) else None
                if source is None and not self.gate_state.initialized:
                    return
                source_time = parse_utc(source["timestamp"]) if source else None
                times = [value for value in (source_time, self.gate_state.next_movement()) if value is not None]
                if self.reassessment_enabled:
                    times.extend(when for _, start, end in self.gate_state.outages
                                 for when in (start, end) if when is not None and when > self.gate_state.now
                                 and (self.gate_state.day_end is None or when < self.gate_state.day_end))
                    if (self.reassessment_at is not None and (self.gate_state.day_end is None
                            or self.reassessment_at < self.gate_state.day_end)):
                        times.append(self.reassessment_at)
                if not times:
                    return
                when = min(times)
                deadline = initialization_seconds + (when - first_time).total_seconds() / self.speed if self.mode == "paced" else None
                if not wait_until(deadline):
                    return
                with self.lock:
                    self.gate_state.advance(when)
                    self._save_gates()
                if self.reassessment_at is not None and self.reassessment_at <= when:
                    self.reassessment_at = None
                if source_time == when:
                    yield index, source
                    index += 1
                elif self.reassessment_enabled:
                    yield None, {"timestamp": utc_string(when), "message": {"action": "ReassessGates"}}

        try:
            with (self.directory / "deliveries.jsonl").open("a", encoding="utf-8") as records:
                for sequence, (index, event) in enumerate(scheduled_events()):
                    event_id = f"{self.id}:{sequence}"
                    deadline = initialization_seconds + (parse_utc(event["timestamp"]) - first_time).total_seconds() / self.speed if self.mode == "paced" else None
                    timestamp = parse_utc(event["timestamp"])
                    with self.lock:
                        if self.stop_event.is_set():
                            break
                        if event["message"]["action"] not in ("InitializeAirport", "ReassessGates"):
                            self.gate_state.apply_message(event["message"], timestamp)
                        payload = self.gate_state.request(event["message"])
                        gate_state_before_decision = copy.deepcopy(self.gate_state.gates)
                        self._save_gates()
                        sent = clock()
                        self.state["attempted" if index is not None else "reassessments_attempted"] += 1
                        self.state["simulated_time"] = event["timestamp"]
                    lateness = 0 if deadline is None else max(0, sent - self.started - deadline)
                    initializing = event["message"]["action"] == "InitializeAirport"
                    record = {
                        "event_id": event_id, "source_event_index": index, "event_type": event["message"]["action"],
                        "simulated_time": event["timestamp"],
                        "intended_deadline_seconds": deadline,
                        "actual_send_seconds": sent - self.started,
                        "actual_send_time": datetime.now(timezone.utc).isoformat(),
                        "lateness_seconds": lateness, "http_status": None, "error": None, "matcher_response": None,
                    }
                    headers = {
                        "X-Stream-Run-Id": self.id,
                        "X-Stream-Event-Id": event_id,
                        "X-Stream-Simulated-Time": event["timestamp"],
                    }
                    if self.mode == "paced":
                        # Lets dashboards advance the simulated clock between events.
                        headers["X-Stream-Speed"] = str(self.speed)
                    if self.resync_pending:
                        headers["X-Stream-Resync"] = "true"
                    record["http_status"], response, attempts, record["error"] = self._deliver(
                        send, payload, headers, initializing=initializing, clock=clock, wait=wait)
                    received_at = clock()
                    record["matcher_response"] = response
                    record["attempts"] = attempts
                    opted_in = isinstance(response, dict) and "reassessment" in response
                    rejected = []
                    if record["error"] is None:
                        try:
                            with self.lock:
                                if initializing or opted_in or self.reassessment_enabled:
                                    self.gate_state.apply_decisions(response, initializing=initializing)
                                else:
                                    rejected = apply_valid_decisions(self.gate_state, response)
                                if opted_in:
                                    self.reassessment_enabled = True
                                if self.reassessment_enabled:
                                    at = response.get("reassessment", {}).get("at") if isinstance(response, dict) else None
                                    self.reassessment_at = parse_utc(at) if at is not None else None
                                self._save_gates()
                        except Exception as error:
                            record["error"] = f"{type(error).__name__}: {error}"
                    record["rejected_decisions"] = rejected
                    if record["error"] is not None:
                        decision_status = "failed" if initializing else "skipped"
                    elif not rejected:
                        decision_status = "applied"
                    else:
                        # An empty rejected group means the response itself was malformed.
                        whole = any(not group["assignments"] and not group["moves"] for group in rejected)
                        decided = sum(len(response.get(key) or []) for key in ("assignments", "moves")) if not whole else 0
                        refused = sum(len(group["assignments"]) + len(group["moves"]) for group in rejected)
                        decision_status = "rejected" if whole or refused >= decided else "partial"
                    record["http_seconds"] = received_at - sent
                    with self.lock:
                        self.state["http_seconds"] += record["http_seconds"]
                        self.state["lateness_seconds"] += lateness
                        self.state["max_lateness_seconds"] = max(self.state["max_lateness_seconds"], lateness)
                        self.state["rejected_decisions"] += sum(
                            len(group["assignments"]) + len(group["moves"]) or 1 for group in rejected)
                        if record["error"] is None:
                            self.state["delivered" if index is not None else "reassessments_delivered"] += 1
                            self.resync_pending = False
                        elif not initializing:
                            self.state["skipped"] += 1
                            self.state["skipped_event_ids"].append(event_id)
                            self.resync_pending = True

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
                        "decision_status": decision_status,
                        "error": record["error"],
                        "attempts": len(attempts),
                        "rejected_decisions": copy.deepcopy(rejected),
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
                    # Without an initialized matcher nothing later can be planned. A failed
                    # update is skipped instead; the next request asks for a resync.
                    if record["error"] and (initializing or opted_in or self.reassessment_enabled or index is None):
                        raise RuntimeError(record["error"])
                    if initializing:
                        initialization_seconds = clock() - self.started
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
