"""Sequential webhook delivery on an anchored simulation clock."""

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

from .validation import DEFAULT_RATE, load_scenario, parse_utc, positive_number, require


WEBHOOK_TIMEOUT = 10
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
        # A response acknowledges transport; matcher computation is measured separately.
        return connection.getresponse().status
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
        self.id = uuid.uuid4().hex
        self.webhook_url = webhook_url
        self.mode = mode
        self.speed = speed
        self.directory = Path(tempfile.mkdtemp(prefix="smartgate-stream-"))
        (self.directory / "scenario.json").write_text(
            json.dumps(self.scenario, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
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
            return {**self.state, "elapsed_seconds": elapsed}

    def stop(self):
        with self.lock:
            if self.state["status"] not in ACTIVE_STATUSES:
                return False
            self.state["status"] = "stopping"
            self.stop_event.set()
            return True

    def start(self):
        self.thread = threading.Thread(target=self.execute, name=f"stream-{self.id}", daemon=True)
        self.thread.start()

    def execute(self, *, clock=time.monotonic, wait=None, send=post_message):
        self.clock = clock
        wait = wait or self.stop_event.wait
        self.started = clock()
        event_id = None
        try:
            with (self.directory / "deliveries.jsonl").open("a", encoding="utf-8") as records:
                for index, event in enumerate(self.scenario["events"]):
                    event_id = f"{self.id}:{index}"
                    deadline = self.offsets[index] if self.mode == "paced" else None
                    if deadline is not None:
                        while not self.stop_event.is_set():
                            delay = self.started + deadline - clock()
                            if delay <= 0:
                                break
                            wait(min(delay, threading.TIMEOUT_MAX))
                    with self.lock:
                        if self.stop_event.is_set():
                            break
                        sent = clock()
                        self.state["attempted"] += 1
                        self.state["simulated_time"] = event["timestamp"]
                    lateness = 0 if deadline is None else max(0, sent - self.started - deadline)
                    record = {
                        "event_id": event_id, "simulated_time": event["timestamp"],
                        "intended_deadline_seconds": deadline,
                        "actual_send_seconds": sent - self.started,
                        "actual_send_time": datetime.now(timezone.utc).isoformat(),
                        "lateness_seconds": lateness, "http_status": None, "error": None,
                    }
                    try:
                        record["http_status"] = send(self.webhook_url, event["message"], {
                            "X-Stream-Run-Id": self.id,
                            "X-Stream-Event-Id": event_id,
                            "X-Stream-Simulated-Time": event["timestamp"],
                        })
                        if not 200 <= record["http_status"] < 300:
                            record["error"] = f"webhook returned HTTP {record['http_status']}"
                    except Exception as error:
                        record["error"] = f"{type(error).__name__}: {error}"
                    record["http_seconds"] = clock() - sent
                    with self.lock:
                        self.state["http_seconds"] += record["http_seconds"]
                        self.state["lateness_seconds"] += lateness
                        self.state["max_lateness_seconds"] = max(self.state["max_lateness_seconds"], lateness)
                        if record["error"] is None:
                            self.state["delivered"] += 1
                    records.write(json.dumps(record, allow_nan=False) + "\n")
                    records.flush()
                    if record["error"]:
                        raise RuntimeError(record["error"])
            with self.lock:
                self.state["status"] = "stopped" if self.stop_event.is_set() else "completed"
                self.finished = clock()
        except Exception as error:
            with self.lock:
                self.state.update(status="failed", failed_event_id=event_id,
                                  error=f"{type(error).__name__}: {error}")
                self.finished = clock()
