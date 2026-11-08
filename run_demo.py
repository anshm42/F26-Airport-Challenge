"""Run the 1,200-flight demo at 60x with both dashboards; Ctrl-C stops everything."""

import contextlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import run_local


ROOT = Path(__file__).resolve().parent


def stop_frontend(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def start_frontend(name, port):
    directory = ROOT / name
    if not (directory / "node_modules" / ".bin" / "vite").is_file():
        subprocess.run(["npm", "ci"], cwd=directory, check=True)
    process = subprocess.Popen(["npm", "run", "dev", "--", "--host", "127.0.0.1",
        "--port", str(port), "--strictPort"], cwd=directory, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return process
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{name} frontend failed to start on port {port}")
    except BaseException:
        stop_frontend(process)
        raise


def main():
    with contextlib.ExitStack() as cleanup:
        for name, port in (("operator", 5173), ("passenger", 5174)):
            cleanup.callback(stop_frontend, start_frontend(name, port))
        print("Operator: http://127.0.0.1:5173/\n"
              "Passenger: http://127.0.0.1:5174/board.html\n"
              "1,200 flights at 60x; about 24 minutes after initialization. Ctrl-C stops all services.", flush=True)
        return run_local.main(["--scenario", str(ROOT / "fixtures" / "pearson-2026-11-07-seed42.json"),
                               "--mode", "paced", "--speed", "60"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        raise SystemExit(1)
