"""Start the local matcher and updater, then replay a scenario."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import matcher_server as matcher
from smartgate_stream.server import ControlServer
from smartgate_stream.validation import positive_number


SCENARIO = Path(__file__).resolve().parent / "fixtures" / "pearson-400-flights-seed42.json"


def find_julia():
    executable = os.environ.get("GMS_JULIA") or shutil.which("julia")
    if not executable:
        juliaup = Path.home() / ".juliaup" / "bin" / "julia"
        if juliaup.is_file():
            executable = str(juliaup)
    if not executable:
        raise RuntimeError("Julia was not found. Add it to PATH or set GMS_JULIA to its executable.")
    return executable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=SCENARIO, help="scenario JSON path (default: checked-in 400-flight day)")
    parser.add_argument("--mode", choices=("paced", "unpaced"), default="paced", help="delivery mode (default: paced)")
    parser.add_argument("--speed", type=float, default=480, help="simulated seconds per real second (default: 480)")
    args = parser.parse_args(argv)
    try:
        speed = positive_number(args.speed, "speed")
    except ValueError as error:
        parser.error(str(error))
    console = sys.stdout
    directory = Path(tempfile.mkdtemp(prefix="smartgate-local-"))
    log_path = directory / "services.log"
    print(f"Service logs: {log_path}", file=console, flush=True)
    try:
        with log_path.open("w", buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            with contextlib.ExitStack() as cleanup:
                # Bind both ports before solving or starting a run; never reuse another server.
                control = cleanup.enter_context(ControlServer(8080))
                receiver = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 9000), matcher.MatcherHandler))
                print("Starting Julia/Gurobi...", file=console, flush=True)
                worker = matcher.JuliaWorker(matcher.SOLVER_BACKEND, find_julia(), matcher.JULIA_TIMEOUT)
                matcher.JULIA_WORKER = worker
                cleanup.callback(worker.close)
                cleanup.callback(setattr, matcher, "JULIA_WORKER", None)
                # Compile both assignment and overlap constraints before the webhook timeout starts.
                print("Warming Julia/Gurobi model...", file=console, flush=True)
                warmup = worker.run({"mode": "initial", "sessions": [
                    {"id": f"warm-{index}", "widthClass": "C", "start": 0, "end": 1} for index in range(2)],
                    "gates": [{"id": f"warm-gate-{index}", "conflictGroup": f"warm-group-{index}",
                               "widthClass": "C", "kind": "gate"} for index in range(2)], "outages": {}})
                if len(warmup.get("assignments", [])) != 2:
                    raise RuntimeError("Julia/Gurobi warm-up did not produce a complete assignment")
                for server in (receiver, control):
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
                    thread.start()
                    cleanup.callback(server.shutdown)
                cleanup.callback(control.manager.close)
                matcher.VIEW_FEED.start()
                cleanup.callback(matcher.VIEW_FEED.stop)
                status = control.manager.create({"scenario_path": str(args.scenario.resolve()),
                    "webhook_url": f"http://127.0.0.1:{receiver.server_port}/updates", "mode": args.mode, "speed": speed})
                run = control.manager.get(status["run_id"])
                run_url = f"http://127.0.0.1:{control.server_port}/runs/{run.id}"
                label = f"Paced replay: {speed:g}x" if args.mode == "paced" else "Unpaced replay"
                print(f"{label}\nRun status: {run_url}\nMatcher: http://127.0.0.1:{receiver.server_port}/assignments"
                      f"\nViews: ws://127.0.0.1:{receiver.server_port}/operator/ws, "
                      f"ws://127.0.0.1:{receiver.server_port}/passenger/ws"
                      f"\nRun records: {run.directory}\nPress Ctrl-C to stop both services.", file=console, flush=True)
                previous = None
                while True:
                    status = run.snapshot()
                    progress = (status["status"], status["delivered"])
                    if progress != previous:
                        print(f"{status['status']}: {status['delivered']}/{status['total_events']} events; "
                              f"{len(status['unassigned_aircraft'])} aircraft waiting", file=console, flush=True)
                        previous = progress
                    if status["status"] not in ("running", "stopping"):
                        break
                    time.sleep(0.5)
                (run.directory / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
                if status["status"] != "completed":
                    raise RuntimeError(f"Replay {status['status']}: {status['error']}")
                print(f"Replay complete: {status['delivered']} events in {status['elapsed_seconds']:.2f}s. "
                      "APIs remain available until Ctrl-C.", file=console, flush=True)
                while True:
                    time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped local services.", file=console, flush=True)
        return 0
    except Exception as error:
        print(f"Error: {error}\nSee {log_path}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
