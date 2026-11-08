# Matcher integration

Run `matcher_adapter.py` from this branch. It connects the stream to the allocator
functions in a separate `ilp_gate_assignment` checkout. Do not start the partner's
`matcher_server.py` HTTP server for this connection.

The adapter uses the stream's flight state, passenger gate map, and movement rules.
The partner's code owns allocation and scoring. No algorithm source is copied or
modified. The supported interface was checked against commit `53d9362`.

## Prepare the algorithm checkout

From this repository, run:

```sh
git fetch origin ilp_gate_assignment
git worktree add --detach /tmp/smartgate-algorithm origin/ilp_gate_assignment
```

If you already have a separate algorithm checkout, use its `matcher_server.py`
path in the commands below. Leave your partner's working checkout and branch alone.

Install Julia and the required packages in the same user account that will start
the adapter. Activate your Gurobi license for that account.

```sh
julia -e 'using Pkg; Pkg.add(["JSON", "JuMP", "Gurobi"])'
```

The default Julia package installation includes the solver binaries. See the
[Julia installation guide](https://julialang.org/downloads/) and
[Gurobi.jl license instructions](https://jump.dev/JuMP.jl/stable/packages/Gurobi/#Install-a-license).

Before replay, test the real backend with one parking session. This checks the
license and package installation outside the receiver's nine-second timeout.

```sh
julia /tmp/smartgate-algorithm/solver_backend.jl <<'JSON'
{
  "mode": "initial",
  "sessions": [{"id": "check", "widthClass": "C", "start": 0, "end": 1}],
  "gates": [{"id": "check-gate", "widthClass": "C", "kind": "gate"}],
  "outages": {}
}
JSON
```

The result must assign `check` to `check-gate`. Resolve license or package errors
before starting a stream run. The adapter uses the algorithm's existing
`GMS_JULIA`, `GMS_JL_BACKEND`, and `GMS_JL_TIMEOUT` settings. The stream's socket
timeout remains ten seconds.
Each initial solve starts a new Julia process. A successful small check does not
prove that a complete day's initial solve will finish within the timeout.

## Start the connection

In one terminal, run:

```sh
python3 matcher_adapter.py \
  --algorithm /tmp/smartgate-algorithm/matcher_server.py \
  --port 9000 > /tmp/matcher-decisions.jsonl
```

The startup address is printed to stderr. Stdout contains one JSON audit record
per accepted event, including its ID, simulated time, decisions, and conflicts.
The server listens on localhost only. Stop it with Ctrl-C.

In a second terminal, run:

```sh
python3 -m smartgate_stream serve --port 8080
```

In a third terminal, start a run:

```sh
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"fixtures/pearson-400-flights-seed42.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"unpaced"}'
```

Use the returned run ID to inspect progress and final stream occupancy:

```sh
curl -sS http://127.0.0.1:8080/runs/RUN_ID
curl -sS http://127.0.0.1:9000/health
curl -sS http://127.0.0.1:9000/assignments
```

`/assignments` contains `gate_state`, `gate_state_time`, `unassigned_aircraft`, and
`planned_assignments` at the last accepted webhook. The stream can advance through
later movements without sending another webhook; its run status and saved
`gate-state.json` remain authoritative. Both matcher endpoints expose `conflicts`.
Stream delivery records contain the exact applied decision responses.

## Integration boundary

The adapter imports these existing members from the selected Python file:

- `SOLVER_BACKEND`: path to the Julia backend.
- `run_julia(backend, payload)`: initial solve, returning session/resource pairs.
- `repair_assignments_python(sessions, gates, outages, previous, changed, starts)`:
  later repair, returning assignments, reassignments, retained assignments, and
  optional gate-entry times.

The adapter does not call the algorithm's HTTP handlers or its schedule parser.
Changing those handlers does not change this connection. Restart the adapter to
load a new algorithm version. If your partner changes the three members above,
update the adapter at that boundary.

Initial occupied gates become fixed reservations. Current occupants remain fixed
until departure unless a known outage requires relocation. The initial solve
plans the remaining sessions. Later repairs use the same allocator functions.
Responses contain only stream `assignments` and explicit `moves`, using complete
flight-leg references and canonical passenger gate labels.

The 110 stream gate keys become 92 solver resources. Each shared swing or H
footprint has one resource. IFATC outage names close that resource through the
existing crosswalk. Solver results are checked for size, occupancy, outage, and
booking conflicts before the state changes. A failed solve or invalid response
retains the previous accepted matcher state and event index.

## V0 limits

- The allocator's width and remote-stand rules remain unchanged. Each H group
  uses its largest position's width class. The same-or-next-class rule can exclude
  smaller aircraft and leave some otherwise usable capacity unused.
- A future gate-entry reservation cannot cause a timed move between webhooks.
  The adapter withholds that plan until a later update can place the waiting
  aircraft. Movement callbacks would be needed for exact entry-time delivery.
- If a current occupant cannot be relocated, its gate remains occupied and
  reserved. The adapter reports `no_immediate_relocation_for_gate_outage` in
  `conflicts`; it does not make the aircraft disappear or free its position.
  This is an unresolved operational conflict, even if transport completes.
- Traffic-zone capability, sterility, jetbridges, and other missing algorithm
  constraints are still outside this integration.

## Checks and rollback

```sh
python3 -m unittest discover -s tests
```

The adapter tests cover the HTTP contract, current positions, shared resources,
outages, cancellations, equipment changes, delayed gate entry, sequencing, and
state retention after failure. They use a stand-in allocator and require no
Julia or Gurobi license. Run the backend check and a complete replay to validate
the real solver in your environment.

To roll back, stop the adapter and use another contract-compatible receiver as
the run's `webhook_url`. The stream API, fixtures, and partner's source files are
unchanged.
