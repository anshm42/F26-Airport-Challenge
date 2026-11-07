import argparse
import json
from datetime import datetime
from pathlib import Path

from .generate import TORONTO, generate
from .server import ControlServer
from .validation import DEFAULT_RATE


def main():
    parser = argparse.ArgumentParser(description="SmartGate dynamic update stream")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the local run-control API")
    serve.add_argument("--port", type=int, default=8080)
    scenario = commands.add_parser("generate", help="write a seeded synthetic Pearson scenario")
    scenario.add_argument("--seed", type=int, default=1)
    scenario.add_argument("--date", default=datetime.now(TORONTO).date().isoformat(),
                          help="Toronto local date, YYYY-MM-DD (default: today in Toronto)")
    scenario.add_argument("--flights", type=int, default=1200, help="number of single-leg flight creations")
    scenario.add_argument("--rate", type=float, default=DEFAULT_RATE,
                          help="simulation speed multiplier for timestamp-paced replay (default: 60)")
    scenario.add_argument("--output", type=Path, required=True, help="new JSON file (must not already exist)")
    args = parser.parse_args()
    if args.command == "generate":
        try:
            fixture = generate(args.seed, args.date, args.flights, rate=args.rate)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(fixture, indent=2, allow_nan=False) + "\n")
        except (ValueError, OSError, OverflowError) as error:
            parser.error(str(error))
        print(f"Wrote {len(fixture['events'])} events to {args.output}")
        return
    with ControlServer(args.port) as server:
        print(f"Stream control: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
