"""Timestamp each stream update and the matcher's answers to it.

The stream's webhook points at this proxy, which forwards every request to the
algorithm unchanged and relays its response. That makes the moment the update
is heard exact. The algorithm's passenger feed is attributed to the update it
answers; feed waitlist episodes are tracked per flight.
"""

import csv
import http.client
import json
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


# The stream writes `action` first; scanning raw bytes avoids parsing a large
# initialization body before forwarding it.
ACTION = re.compile(rb'"action"\s*:\s*"([A-Za-z]+)"')
FORWARD_HEADERS = ("Content-Type", "X-Stream-Run-Id", "X-Stream-Event-Id", "X-Stream-Simulated-Time")
FORWARD_TIMEOUT = 60
ATTRIBUTION_LOOKBACK = 10


def epoch_seconds(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def percentile(values, fraction):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    return values[min(len(values) - 1, round(fraction * (len(values) - 1)))]


class Recorder:
    """Measurements for one algorithm during one run."""

    def __init__(self, algorithm, webhook_url):
        self.algorithm = algorithm
        self.webhook_url = webhook_url
        self.lock = threading.Lock()
        self.events = []
        self.flights = {}
        self.feed_messages = 0
        self.stale_feed_messages = 0

    def heard(self, headers, action, size, at):
        with self.lock:
            event = {
                "index": len(self.events), "event_id": headers.get("X-Stream-Event-Id"), "action": action,
                "simulated_time": headers.get("X-Stream-Simulated-Time"), "request_bytes": size,
                "heard": at, "answered": None, "http_status": None, "response_bytes": None,
                "assignments": None, "moves": None, "error": None, "feed_first": None, "feed_messages": 0,
            }
            self.events.append(event)
            return event

    def answered(self, event, at, status, body, error=None):
        assignments = moves = None
        if status == 200 and body:
            try:
                decisions = json.loads(body)
                assignments, moves = len(decisions.get("assignments", [])), len(decisions.get("moves", []))
            except (ValueError, AttributeError):
                pass
        with self.lock:
            event.update(answered=at, http_status=status, response_bytes=len(body), assignments=assignments,
                         moves=moves, error=error or (None if status is not None and 200 <= status < 300
                                                      else f"HTTP {status}"))

    def feed(self, at, payload):
        try:
            message = json.loads(payload)
            generated = epoch_seconds(message["generatedAt"]) if message.get("generatedAt") else None
        except (ValueError, KeyError, TypeError):
            return
        with self.lock:
            if not self.events:
                self.stale_feed_messages += 1  # snapshot left over from before this run
                return
            self.feed_messages += 1
            event = self.events[-1]
            # Prefer the latest update whose simulated time the feed reports, in case this
            # message was read after the next update had already arrived.
            for candidate in reversed(self.events[-ATTRIBUTION_LOOKBACK:]):
                if generated is not None and epoch_seconds(candidate["simulated_time"]) == generated:
                    event = candidate
                    break
            if event["feed_first"] is None:
                event["feed_first"] = at
            event["feed_messages"] += 1
            simulated = epoch_seconds(event["simulated_time"])
            flights = message.get("flights", [])
            for flight in flights:
                self._track(flight, simulated, at)
            # A flight that leaves the board (departed or retired) is no longer waiting.
            shown = {flight["flightId"] for flight in flights}
            for fid, record in self.flights.items():
                if record["since_simulated"] is not None and fid not in shown:
                    self._close(record, simulated, at)

    def _track(self, flight, simulated, at):
        record = self.flights.setdefault(flight["flightId"], {
            "code": (flight.get("flightCodes") or [flight["flightId"]])[0], "episodes": 0,
            "waited_simulated": 0.0, "longest_simulated": 0.0, "waited_wall": 0.0,
            "since_simulated": None, "since_wall": None, "reported_waitlisted_since": None,
            "reported_assigned_at": None,
        })
        if flight.get("waitlistedSince"):
            record["reported_waitlisted_since"] = flight["waitlistedSince"]
        if flight.get("assignedAt"):
            record["reported_assigned_at"] = flight["assignedAt"]
        waiting = flight.get("gate") is None and not flight.get("cancelled")
        if waiting and record["since_simulated"] is None:
            record["episodes"] += 1
            record["since_simulated"], record["since_wall"] = simulated, at
        elif not waiting and record["since_simulated"] is not None:
            self._close(record, simulated, at)

    @staticmethod
    def _close(record, simulated, at):
        waited = simulated - record["since_simulated"]
        record["waited_simulated"] += waited
        record["longest_simulated"] = max(record["longest_simulated"], waited)
        record["waited_wall"] += at - record["since_wall"]
        record["since_simulated"] = record["since_wall"] = None

    def finalize(self, directory, final_simulated=None):
        """Write events.csv and waitlist.csv; return the summary metrics."""
        with self.lock:
            events, flights = [dict(event) for event in self.events], self.flights
            end_wall = time.perf_counter()
            end_simulated = final_simulated or (epoch_seconds(events[-1]["simulated_time"]) if events else None)
            unresolved = 0
            for record in flights.values():
                if record["since_simulated"] is not None:
                    unresolved += 1
                    self._close(record, max(end_simulated, record["since_simulated"]), end_wall)
        start = events[0]["heard"] if events else 0
        with (directory / "events.csv").open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["index", "event_id", "action", "simulated_time", "request_bytes", "heard_seconds",
                             "response_seconds", "feed_latency_seconds", "feed_messages", "http_status",
                             "response_bytes", "assignments", "moves", "error"])
            for event in events:
                event["response_seconds"] = event["answered"] - event["heard"] if event["answered"] else None
                event["feed_seconds"] = event["feed_first"] - event["heard"] if event["feed_first"] else None
                writer.writerow([event["index"], event["event_id"], event["action"], event["simulated_time"],
                                 event["request_bytes"], round(event["heard"] - start, 6),
                                 _round(event["response_seconds"]), _round(event["feed_seconds"]),
                                 event["feed_messages"], event["http_status"], event["response_bytes"],
                                 event["assignments"], event["moves"], event["error"]])
        waitlisted = {fid: record for fid, record in flights.items() if record["episodes"]}
        with (directory / "waitlist.csv").open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["flight_id", "flight_code", "episodes", "waited_simulated_minutes",
                             "longest_simulated_minutes", "waited_wall_seconds", "reported_waitlisted_since",
                             "reported_assigned_at"])
            for fid, record in sorted(waitlisted.items()):
                writer.writerow([fid, record["code"], record["episodes"], round(record["waited_simulated"] / 60, 3),
                                 round(record["longest_simulated"] / 60, 3), round(record["waited_wall"], 6),
                                 record["reported_waitlisted_since"], record["reported_assigned_at"]])
        initial = events[0] if events and events[0]["action"] == "InitializeAirport" else None
        updates = [event for event in events if event is not initial]
        responses = [event["response_seconds"] for event in updates]
        feeds = [event["feed_seconds"] for event in updates]
        waits = [record["waited_simulated"] / 60 for record in waitlisted.values()]
        return {
            "events_heard": len(events),
            "init_request_bytes": initial["request_bytes"] if initial else None,
            "init_response_seconds": _round(initial["response_seconds"]) if initial else None,
            "init_feed_seconds": _round(initial["feed_seconds"]) if initial else None,
            "update_response_p50_seconds": _round(percentile(responses, 0.5)),
            "update_response_p95_seconds": _round(percentile(responses, 0.95)),
            "update_response_max_seconds": _round(percentile(responses, 1)),
            "update_feed_p50_seconds": _round(percentile(feeds, 0.5)),
            "update_feed_p95_seconds": _round(percentile(feeds, 0.95)),
            "update_feed_max_seconds": _round(percentile(feeds, 1)),
            "updates_without_feed": sum(value is None for value in feeds),
            "feed_messages": self.feed_messages,
            "waitlisted_flights": len(waitlisted),
            "waitlist_episodes": sum(record["episodes"] for record in waitlisted.values()),
            "waitlist_total_minutes": round(sum(waits), 3),
            "waitlist_mean_minutes": round(sum(waits) / len(waits), 3) if waits else None,
            "waitlist_max_minutes": round(max(waits), 3) if waits else None,
            "waitlist_unresolved": unresolved,
        }


def _round(value):
    return None if value is None else round(value, 6)


def forward(url, body, headers):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=FORWARD_TIMEOUT)
    try:
        connection.request("POST", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), body, headers)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


class WebhookProxy(ThreadingHTTPServer):
    """One listener for every stream run; `/webhook/<algorithm>` routes to that algorithm's recorder."""

    daemon_threads = True

    def __init__(self, port=0):
        self.recorders = {}
        super().__init__(("127.0.0.1", port), ProxyHandler)

    def url(self, algorithm):
        return f"http://127.0.0.1:{self.server_port}/webhook/{algorithm}"


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        path = urlsplit(self.path).path.strip("/").split("/")
        recorder = self.server.recorders.get(path[1]) if len(path) == 2 and path[0] == "webhook" else None
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        heard = time.perf_counter()
        if recorder is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        match = ACTION.search(body)
        headers = {name: self.headers[name] for name in FORWARD_HEADERS if self.headers.get(name)}
        event = recorder.heard(headers, match.group(1).decode() if match else None, len(body), heard)
        try:
            status, content_type, data = forward(recorder.webhook_url, body, headers)
        except OSError as error:
            recorder.answered(event, time.perf_counter(), None, b"", error=f"{type(error).__name__}: {error}")
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        answered = time.perf_counter()
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        recorder.answered(event, answered, status, data)
