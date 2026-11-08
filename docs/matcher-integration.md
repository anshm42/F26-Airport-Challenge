# Matching branch integration

## One-command local replay

When this checkout contains `matcher_server.py` and `run_local.py`, run:

```sh
python3 run_local.py
```

This starts both local HTTP services and the persistent Julia worker, then runs
the 400-flight fixture in paced mode at 480×. It prints progress and the run URL.
The replay takes about three minutes. After completion, press Ctrl-C to stop the
services. Julia packages and a working Gurobi license must already be installed.
Use `python3 run_local.py --speed 60` to slow the replay, or supply any other
finite positive multiplier with `--speed`. Logs and run records remain on disk after
shutdown. The script does not reuse services already listening on ports 8080/9000.

For the 1,200-flight fixture in unpaced mode:

```sh
python3 run_local.py --scenario fixtures/pearson-2026-11-07-seed42.json --mode unpaced
```

`--speed` controls paced runs; unpaced runs wait only for each matcher response.

## Manual setup with separate checkouts

The active matcher and allocator are on the **`matching` branch**. Run the stream
from `integration` and the matcher from a separate `matching` checkout. The
matching branch now provides its own stream-compatible HTTP receiver.

The separate-checkout instructions were originally checked against `90f9089`.
Leave your partner's working checkout and branch alone.

## Prepare the matching checkout

From this repository, run:

```sh
git fetch origin matching
git worktree add --detach /tmp/smartgate-algorithm origin/matching
```

If you already have a separate matching checkout, use its path below. To refresh
the detached checkout after your partner pushes, stop the matcher and run:

```sh
git fetch origin matching
git -C /tmp/smartgate-algorithm switch --detach origin/matching
```

Restart the matcher after the checkout changes.

## Check Julia and Gurobi

If you installed Julia with Juliaup, make sure the terminal can find it:

```sh
export PATH="$HOME/.juliaup/bin:$PATH"
julia --version
```

The matcher inherits this `PATH` and uses `julia` to start the backend. You can
also set `GMS_JULIA` to the full Julia executable path.

If the packages and license are already set up, skip installation. On another
machine, install the packages in the same user account that will run the matcher:

```sh
julia -e 'using Pkg; Pkg.add(["JSON", "JuMP", "Gurobi"])'
```

The Julia package installation includes the solver binaries. See the
[Julia installation guide](https://julialang.org/downloads/) and
[Gurobi.jl license instructions](https://jump.dev/JuMP.jl/stable/packages/Gurobi/#Install-a-license).

Test the real matching backend with one parking session:

```sh
julia /tmp/smartgate-algorithm/solver_backend.jl <<'JSON'
{"mode":"initial","sessions":[{"id":"check","widthClass":"C","start":0,"end":1}],"gates":[{"id":"check-gate","conflictGroup":"check-group","widthClass":"C","kind":"gate"}],"outages":{}}
JSON
```

The result must assign `check` to `check-gate`. Resolve license or package errors
before replay. The matcher uses `GMS_JULIA`, `GMS_JL_BACKEND`, and
`GMS_JL_TIMEOUT`; its default Julia timeout is nine seconds. The stream's socket
timeout is ten seconds. The current receiver starts one persistent Julia worker
before accepting webhooks and allows up to 120 seconds for worker startup. A
small successful check does not prove that a complete day's solve meets the
per-request timeout.

## Start the connection

In one terminal, start the matching branch's receiver:

```sh
export PATH="$HOME/.juliaup/bin:$PATH"
python3 /tmp/smartgate-algorithm/matcher_server.py \
  --host 127.0.0.1 --port 9000 > /tmp/matcher.log
```

In a second terminal, start the stream from this repository:

```sh
python3 -m smartgate_stream serve --port 8080
```

In a third terminal, start a run:

```sh
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"fixtures/pearson-400-flights-seed42.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"unpaced"}'
```

Use the returned run ID to inspect progress and final occupancy:

```sh
curl -sS http://127.0.0.1:8080/runs/RUN_ID
curl -sS http://127.0.0.1:9000/health
curl -sS http://127.0.0.1:9000/assignments
```

The stream owns physical occupancy. Its run status and saved `gate-state.json`
are authoritative, including movements after the last webhook. Stream delivery
records contain the exact matcher responses. The matcher owns planned assignments
and dashboard data. See the matching branch's
[matcher API](https://github.com/anshm42/F26-Airport-Challenge/blob/matching/docs/matcher-api.md)
for its inspection endpoints and passenger WebSocket feed.

## Adapter status on integration

`matcher_adapter.py` remains on `integration`. It was tested against the earlier
v0 interface at commit `53d9362` and has not been adapted to current `matching`.

The current matching allocator requires `gates[].conflictGroup` and a started
persistent Julia worker. The adapter does not yet provide that setup. It needs
an update and new compatibility checks before use with current `matching`.
Use `run_local.py` or the matching branch's own receiver as shown above.

The earlier v0 width and remote-stand restrictions no longer describe current
`matching`: the current allocator accepts a mapped gate whose maximum width class
fits the aircraft and permits H positions with a soft remote-parking penalty.

## Checks and rollback

```sh
python3 -m unittest discover -s tests
```

The adapter tests on this branch use a stand-in allocator. They do not validate
the latest matching branch or the real Julia solve. Run the backend check and a
complete replay when validating a new matcher revision.

Run the real solver's parked-aircraft regression separately with an installed
Julia/Gurobi environment and a valid license:

```sh
RUN_GUROBI_TESTS=1 python3 -m unittest discover -s tests -p test_solver_occupancy.py -v
```

The cases check that an occupied gate keeps its reservation, later arrivals
use another compatible gate when one is available, and a departure releases the
gate. They also check shared conflict groups and invalid mandatory reservations.
These checks are opt-in so the ordinary suite does not require a Gurobi license.

To roll back a matcher revision, stop its process and restore the previous
detached checkout revision. Restart the matcher and start a new stream run.
