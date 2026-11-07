"""
JuMP/Gurobi solution adapter for the GMS evaluator.

The evaluator imports this module and calls decide(observation).
The adapter sends that observation to solver_backend.jl and converts the
JSON response into the tuple-list format expected by the evaluator.

Configuration:
  GMS_JULIA       Optional path/name of the Julia executable (default: julia)
  GMS_JL_BACKEND  Optional path to solver_backend.jl
  GMS_JL_TIMEOUT  Optional subprocess timeout in seconds (default: 45)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
_BACKEND = Path(os.environ.get("GMS_JL_BACKEND", _HERE / "solver_backend.jl"))
_JULIA = os.environ.get("GMS_JULIA", "julia")
_TIMEOUT = float(os.environ.get("GMS_JL_TIMEOUT", "45"))


def decide(observation: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """
    Solve one evaluator decision tick.

    Returns:
        {
            "assignments": [(flight_id, gate_id), ...],
            "reassignments": [(flight_id, gate_id), ...],
        }
    """
    if not isinstance(observation, dict):
        raise TypeError("decide(observation) expects a dictionary.")

    if not _BACKEND.is_file():
        raise FileNotFoundError(
            f"Could not find Julia backend: {_BACKEND}. "
            "Place solver_backend.jl beside solution_ilp.py or set GMS_JL_BACKEND."
        )

    payload = json.dumps(observation, separators=(",", ":"))

    try:
        completed = subprocess.run(
            [_JULIA, str(_BACKEND)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT,
            cwd=str(_HERE),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Could not launch Julia executable '{_JULIA}'. "
            "Install Julia and ensure 'julia' is on PATH, or set GMS_JULIA."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Julia gate optimizer exceeded the {_TIMEOUT:g}-second timeout."
        ) from exc

    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "Julia gate optimizer failed"
            + (f":\n{details}" if details else " (no error details were returned).")
        )

    # The backend writes exactly one JSON object to stdout. Any diagnostics
    # are sent to stderr so they cannot corrupt this response.
    # Gurobi may print license/startup messages to stdout before the
    # backend's JSON response. Find and decode the first valid JSON object
    # rather than requiring stdout to contain JSON and nothing else.
    response = None
    decoder = json.JSONDecoder()
    for position, character in enumerate(completed.stdout):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(completed.stdout[position:])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(candidate, dict)
            and "assignments" in candidate
            and "reassignments" in candidate
        ):
            response = candidate
            break

    if response is None:
        raise RuntimeError(
            "Julia backend did not return a recognizable JSON response. "
            f"stdout was: {completed.stdout[:1500]!r}; "
            f"stderr was: {completed.stderr[:1500]!r}"
        )

    def convert_pairs(key: str) -> list[tuple[str, str]]:
        pairs = response.get(key, [])
        if not isinstance(pairs, list):
            raise TypeError(f"Julia response field {key!r} must be a list.")
        converted = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(
                    f"Invalid {key} entry from Julia backend: {pair!r}"
                )
            converted.append((str(pair[0]), str(pair[1])))
        return converted

    return {
        "assignments": convert_pairs("assignments"),
        "reassignments": convert_pairs("reassignments"),
    }
