"""Local HTTP control plane; flight state and gate assignments belong to the matcher."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .replay import ACTIVE_STATUSES, WEBHOOK_TIMEOUT, Run
from .validation import read_json, require


class ConflictError(Exception):
    pass


class RunManager:
    def __init__(self):
        self.runs = {}
        self.lock = threading.Lock()
        self.closed = False

    def create(self, options):
        require(isinstance(options, dict), "request must be an object")
        require(set(options) <= {"scenario_path", "webhook_url", "mode", "speed"}, "unknown run option")
        require(isinstance(options.get("scenario_path"), str) and bool(options["scenario_path"]),
                "scenario_path must be a nonempty file path")
        require("webhook_url" in options, "webhook_url is required")
        with self.lock:
            if self.closed or any(run.snapshot()["status"] in ACTIVE_STATUSES for run in self.runs.values()):
                raise ConflictError("another run is active or the server is shutting down")
            run = Run(**options)
            self.runs[run.id] = run
            run.start()
            return run.snapshot()

    def get(self, run_id):
        with self.lock:
            return self.runs[run_id]

    def close(self):
        with self.lock:
            self.closed = True
            runs = list(self.runs.values())
            for run in runs:
                run.stop()
        for run in runs:
            if run.thread is not None:
                run.thread.join(WEBHOOK_TIMEOUT + 1)


class ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, body):
        data = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def stream_events(self, run, after):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                events = run.wait_for_events(after, timeout=15)
                if events:
                    for event in events:
                        after = event["sequence"]
                        payload = json.dumps(event, allow_nan=False, separators=(",", ":"))
                        frame = f"id: {after}\nevent: decision\ndata: {payload}\n\n"
                        self.wfile.write(frame.encode("utf-8"))
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                if run.snapshot()["status"] not in ACTIVE_STATUSES and not run.events_after(after):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True

    def dispatch(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.strip("/").split("/")
            query = parse_qs(parsed.query)
            if self.command == "POST" and path == ["runs"]:
                require(self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json",
                        "Content-Type must be application/json")
                require(self.headers.get("Transfer-Encoding") is None, "chunked requests are not supported")
                length = int(self.headers.get("Content-Length", "0"))
                require(0 < length <= 1024 * 1024, "request body must be between 1 byte and 1 MiB")
                body = self.rfile.read(length)
                require(len(body) == length, "incomplete request body")
                self.respond(201, self.server.manager.create(read_json(body)))
            elif self.command == "GET" and len(path) == 2 and path[0] == "runs":
                self.respond(200, self.server.manager.get(path[1]).snapshot())
            elif self.command == "GET" and len(path) == 3 and path[0] == "runs" and path[2] == "events":
                after = int(query.get("after", ["0"])[0])
                require(after >= 0, "after must be a nonnegative event sequence")
                run = self.server.manager.get(path[1])
                events = run.events_after(after)
                self.respond(200, {
                    "run_id": path[1],
                    "events": events,
                    "latest_sequence": events[-1]["sequence"] if events else after,
                })
            elif (self.command == "GET" and len(path) == 4 and path[0] == "runs"
                  and path[2] == "events" and path[3] == "stream"):
                after = int(query.get("after", ["0"])[0])
                require(after >= 0, "after must be a nonnegative event sequence")
                self.stream_events(self.server.manager.get(path[1]), after)
            elif self.command == "POST" and len(path) == 3 and path[0] == "runs" and path[2] == "stop":
                run = self.server.manager.get(path[1])
                if not run.stop():
                    raise ConflictError("run has already finished")
                self.respond(202, run.snapshot())
            else:
                self.respond(404, {"error": "unknown endpoint"})
        except KeyError:
            self.respond(404, {"error": "unknown run"})
        except ConflictError as error:
            self.respond(409, {"error": str(error)})
        except (ValueError, UnicodeError, OSError, RecursionError) as error:
            self.respond(400, {"error": str(error)})


class ControlServer(ThreadingHTTPServer):
    def __init__(self, port=8080):
        self.manager = RunManager()
        super().__init__(("127.0.0.1", port), ControlHandler)

    def server_close(self):
        self.manager.close()
        super().server_close()
