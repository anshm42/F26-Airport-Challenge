"""Drive dynamic-update runs against each algorithm and collect the measurements."""

import csv
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .feed import FeedListener
from .recorder import Recorder, WebhookProxy


REPOSITORY = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"completed", "stopped", "failed"}
STATUS_FIELDS = ("run_id", "status", "error", "failed_event_id", "mode", "speed", "total_events", "attempted",
                 "delivered", "elapsed_seconds", "http_seconds", "lateness_seconds", "max_lateness_seconds",
                 "simulated_time", "gate_state_time", "records_directory")
SUMMARY_FIELDS = ("run", "algorithm", "flights", "mode", "speed", "status", "error", "total_events",
                  "delivered", "elapsed_seconds", "max_lateness_seconds")


def request_json(method, url, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def wait_for(check, timeout, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return True
        except OSError:
            pass
        time.sleep(interval)
    return False


def port_open(port):
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        return True


class Harness:
    def __init__(self, config, *, algorithms=None, runs=None):
        self.config = config
        self.algorithms = {name: settings for name, settings in config["algorithms"].items()
                           if algorithms is None or name in algorithms}
        self.runs = [run for run in config["runs"] if runs is None or run["name"] in runs]
        self.output = Path(config.get("output_dir", "harness-results")).resolve()
        self.session = self.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.streams = {}
        self.available = {}
        self.rows = []

    def log(self, text):
        print(text, flush=True)

    def start(self, stack):
        (self.session / "logs").mkdir(parents=True)
        (self.session / "config.json").write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
        for index, (name, settings) in enumerate(self.algorithms.items()):
            if "stream_url" in settings:
                self.streams[name] = settings["stream_url"].rstrip("/")
            else:
                port = settings.get("stream_port", 8701 + index)
                self.spawn(stack, f"stream-{name}", [sys.executable, "-m", "smartgate_stream", "serve", "--port", str(port)])
                if not wait_for(lambda: port_open(port), 10):
                    raise RuntimeError(f"stream server for {name} did not open port {port}")
                self.streams[name] = f"http://127.0.0.1:{port}"
            if "command" in settings:
                self.spawn(stack, name, [sys.executable if part == "{python}" else part for part in settings["command"]])
            health = settings.get("health_url")
            self.available[name] = health is None or wait_for(
                lambda: request_json("GET", health, timeout=2)[0] == 200, self.config.get("startup_timeout_seconds", 30))
            if not self.available[name]:
                self.log(f"{name}: not reachable at {health}; its runs are recorded as unavailable")
        proxy = stack.enter_context(WebhookProxy(self.config.get("harness_port", 0)))
        threading.Thread(target=proxy.serve_forever, daemon=True, name="webhook-proxy").start()
        stack.callback(proxy.shutdown)
        return proxy

    def spawn(self, stack, name, command):
        log = stack.enter_context((self.session / "logs" / f"{name}.log").open("w", encoding="utf-8"))
        process = subprocess.Popen(command, cwd=REPOSITORY, stdout=log, stderr=subprocess.STDOUT)

        def stop():
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        stack.callback(stop)

    def fixture(self, run):
        if "scenario" in run:
            return Path(run["scenario"]).resolve()
        options = {"seed": 1, "rate": 60, **run["generate"]}
        name = "-".join(f"{key}-{options[key]}" for key in sorted(options)) + ".json"
        path = self.output / "fixtures" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-m", "smartgate_stream", "generate", "--output", str(path)]
            for key in ("seed", "date", "flights", "rate"):
                if key in options:
                    command += [f"--{key}", str(options[key])]
            result = subprocess.run(command, cwd=REPOSITORY, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else "generation failed")
        return path

    def execute(self, proxy, run, name, fixture, flights):
        settings = self.algorithms[name]
        directory = self.session / "runs" / run["name"] / name
        directory.mkdir(parents=True)
        row = {"run": run["name"], "algorithm": name, "flights": flights, "mode": run.get("mode", "unpaced"),
               "speed": run.get("speed")}
        if not self.available[name]:
            return {**row, "status": "unavailable"}
        recorder = Recorder(name, settings["webhook_url"])
        proxy.recorders[name] = recorder
        listener = FeedListener(settings["websocket_url"], recorder.feed)
        listener.start()
        listener.connected.wait(10)
        if listener.error:
            self.log(f"{name}: feed unavailable ({listener.error}); recording webhook timing only")
        body = {"scenario_path": str(fixture), "webhook_url": proxy.url(name), "mode": row["mode"]}
        if "speed" in run:
            body["speed"] = run["speed"]
        code, status = request_json("POST", f"{self.streams[name]}/runs", body)
        if code != 201:
            listener.close()
            return {**row, "status": "rejected", "error": (status or {}).get("error")}
        url = f"{self.streams[name]}/runs/{status['run_id']}"
        deadline = time.monotonic() + run.get("timeout_seconds", self.config.get("run_timeout_seconds", 3600))
        poll = self.config.get("status_poll_seconds", 0.1)
        while True:
            # Status polls add tarmac observations between webhook updates; denser in paced runs.
            if status.get("gate_state_time"):
                recorder.observe(status["gate_state_time"],
                                 (plane["registration"] for plane in status["unassigned_aircraft"]), 1)
            if status["status"] in TERMINAL_STATUSES:
                break
            time.sleep(poll)
            if time.monotonic() > deadline and status["status"] == "running":
                request_json("POST", f"{url}/stop")
            status = request_json("GET", url)[1]
        time.sleep(self.config.get("feed_settle_seconds", 0.5))
        listener.close()
        status = {field: status.get(field) for field in STATUS_FIELDS}
        (directory / "stream-status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        return {**row, **{field: status.get(field) for field in SUMMARY_FIELDS if field in status},
                **recorder.finalize(directory)}

    def write_summary(self):
        fields = list(dict.fromkeys(field for row in self.rows for field in [*SUMMARY_FIELDS, *row]))
        with (self.session / "summary.csv").open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fields)
            writer.writeheader()
            writer.writerows(self.rows)
        (self.session / "summary.json").write_text(json.dumps(self.rows, indent=2) + "\n", encoding="utf-8")

    def run(self):
        with ExitStack() as stack:
            proxy = self.start(stack)
            self.log(f"Results: {self.session}")
            for run in self.runs:
                names = [name for name in run.get("algorithms", self.algorithms) if name in self.algorithms]
                try:
                    fixture = self.fixture(run)
                    scenario = json.loads(fixture.read_text(encoding="utf-8"))
                    flights = scenario.get("metadata", {}).get("flight_count") or len(scenario["events"][0]["message"].get("flights", []))
                except (OSError, ValueError, RuntimeError) as error:
                    self.log(f"[{run['name']}] fixture unavailable: {error}")
                    self.rows += [{"run": run["name"], "algorithm": name, "status": "no-fixture", "error": str(error)}
                                  for name in names]
                    self.write_summary()
                    continue
                self.log(f"[{run['name']}] {flights} flights, {run.get('mode', 'unpaced')}"
                         + (f" at {run['speed']}x" if "speed" in run else "") + f": {', '.join(names)}")
                results = {}

                def attempt(name):
                    try:
                        results[name] = self.execute(proxy, run, name, fixture, flights)
                    except Exception as error:
                        results[name] = {"run": run["name"], "algorithm": name, "flights": flights,
                                         "status": "harness-error", "error": f"{type(error).__name__}: {error}"}
                if self.config.get("parallel", True):
                    threads = [threading.Thread(target=attempt, args=(name,)) for name in names]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()
                else:
                    for name in names:
                        attempt(name)
                for name in names:
                    row = results[name]
                    self.rows.append(row)
                    self.log(f"  {name}: {row.get('status')}" + "".join(
                        f", {label} {row[key]}" for label, key in (
                            ("init", "init_response_seconds"), ("response p50", "update_response_p50_seconds"),
                            ("feed p50", "update_feed_p50_seconds"), ("unassigned flights", "unassigned_flights"),
                            ("tarmac aircraft", "tarmac_aircraft"), ("tarmac minutes", "tarmac_total_minutes"),
                            ("error", "error"))
                        if row.get(key) is not None))
                self.write_summary()
        return self.session
