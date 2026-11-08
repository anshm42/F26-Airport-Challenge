# SmartGate webhook matcher (stream branch)

These are **new matcher files**. They do not replace or modify the repository's
`smartgate_stream` updater, evaluator, visualizer, tests, or other files.

## Files and placement

Copy these into the repository:

- `matcher_server.py` → repository root
- `solver_backend.jl` → repository root
- `station_timezones.json` → `data/station_timezones.json`

The matcher reads the existing `data/pearson.json` and `data/aircraft.json`.
It has no Python third-party dependencies; the HTTP server uses Python's
standard library.

## Julia dependencies

Install Julia packages once:

```sh
julia -e 'using Pkg; Pkg.add(["JSON", "JuMP", "Gurobi"])'
```

Gurobi must also be installed/licensed in the environment where Julia runs.
The matcher launches Julia once for the initial solve. Later updates are
repaired in Python so the receiver does not repeatedly pay Julia startup cost.

Warm up package loading before starting a replay (recommended):

```sh
echo '{"mode":"initial","sessions":[],"gates":[],"outages":{}}' | julia solver_backend.jl
```

If Julia is not on `PATH`, set `GMS_JULIA` to the Julia executable. If needed,
set `GMS_JL_TIMEOUT` (default 9 seconds; the updater's HTTP timeout is 10 seconds)
and `GMS_JL_BACKEND` to the backend path.

## Run the matcher and updater

Use three terminals from the repository root.

**Terminal 1 — matcher receiver:**

```sh
python matcher_server.py --host 127.0.0.1 --port 9000
```

**Terminal 2 — stream control server:**

```sh
python3 -m smartgate_stream serve --port 8080
```

**Terminal 3 — start a replay:**

```sh
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"fixtures/pearson-2026-11-07-seed42.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"unpaced"}'
```

Use the actual scenario path present in your checkout. If you are replaying the
uploaded `pearson-400-flights-seed42.json`, set `scenario_path` to its path.

Check matcher status and current allocations:

```sh
curl http://127.0.0.1:9000/health
curl http://127.0.0.1:9000/assignments
```

The stream server's `/runs` response includes a `run_id`. Use it to inspect
replay progress:

```sh
curl http://127.0.0.1:8080/runs/RUN_ID
```

## What this version does

- Accepts one raw JSON message per `POST /updates`, using the required
  `X-Stream-Run-Id`, `X-Stream-Event-Id`, and `X-Stream-Simulated-Time` headers.
- Requires `InitializeAirport` as event index 0, builds a fresh state, runs one
  joint JuMP/Gurobi ILP, and only then acknowledges with HTTP 200.
- Applies later events sequentially and returns HTTP 204 after each successful
  state transition.
- Handles `CreateFlight`, `ReplaceFlight`, `UpdateTiming`, `CancelFlight`,
  `ReinstateFlight`, `UpdateEquipment`, `GateOutage`, and `DivertFlight`.
- Preserves unaffected assignments and greedily repairs only changed parking
  sessions. It avoids re-solving the full ILP after every update.
- Uses IANA timezones for the station codes in the supplied seeded fixture. Add
  a timezone entry to `data/station_timezones.json` if a new YYZ-relevant station
  appears in another scenario; an unknown required mapping rejects the message
  rather than silently dropping that parking interval.
- Exposes `/health` and `/assignments` for inspection.

## Important model limitations

1. **Width classes are a simulator heuristic, not operational clearance.** The
   current candidate rule permits the same IFATC width class or the next larger
   class. The repository's static-data README explicitly says these classes are
   not verified per-stand approvals. Do not use these assignments for real
   airport operations.
2. **Passenger-label outage crosswalk is not supplied.** An outage such as
   `D45`, `B4`, or `A13` does not exactly identify an IFATC stand in the supplied
   static JSON. The matcher records these as unmatched and does not guess that
   `D45` means `Gate 45`. Supply an authoritative crosswalk before relying on
   those outage events.
3. The current model does not implement passenger-zone / swing-gate sterility,
   terminal customs routing, jetbridge requirements, adjacent-stand blocking,
   or all airline/service-role restrictions. It uses named `Gate ...` resources
   for ordinary sessions and `Remote Stand ...` resources only when diversion
   policy allows remote parking.
4. `station_timezones.json` covers the 30 station codes in the checked-in seed
   scenario. It is a compact fixture lookup, not a comprehensive airport
   database.
5. This implementation has been Python syntax-checked and its state/update
   handling has been smoke-tested across the 158 updates in the supplied
   400-flight fixture using a stand-in initial allocator. **The JuMP/Gurobi
   model has not been executed in this environment**, so run the warm-up and a
   full replay locally and inspect the returned `/assignments` and run records.

## Useful checks

From the repository root, run the repository's existing tests:

```sh
python3 -m unittest discover -s tests -v
```

The matcher files are additive and do not change those tests.
