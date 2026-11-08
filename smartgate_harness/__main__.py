import argparse
import json
from pathlib import Path

from .runner import Harness


def main():
    parser = argparse.ArgumentParser(description="Benchmark matching algorithms against dynamic-update runs")
    parser.add_argument("config", type=Path, help="harness run configuration (JSON)")
    parser.add_argument("--algorithms", help="comma-separated subset of configured algorithms")
    parser.add_argument("--runs", help="comma-separated subset of configured run names")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    split = lambda value: set(value.split(",")) if value else None
    Harness(config, algorithms=split(args.algorithms), runs=split(args.runs)).run()


if __name__ == "__main__":
    main()
