# SmartGate

The dynamic update server replays synthetic Pearson flight updates to a matching
algorithm over an HTTP(S) webhook. The matcher chooses future gates; the stream
owns current passenger-gate occupancy and simulated aircraft movements.

## Run locally with one command

For the complete **1,200-flight demo at 60×**, including both frontends:

```sh
python3 run_demo.py
```

Open the operator at `http://127.0.0.1:5173/` and passenger board at
`http://127.0.0.1:5174/board.html`. The launcher installs locked frontend
dependencies with `npm ci` if needed (Node/npm required). Julia/Gurobi requirements
below still apply. Ports 8080, 9000, 5173, and 5174 must be free. Initialization
finishes before the simulation clock starts; a complete day takes about 24 minutes.
Ctrl-C stops both frontends, both backend services, and Julia. Run the same command
again for a fresh day. To show a selected gate, use the passenger setup page at
`http://127.0.0.1:5174/`.

The native matcher opts into automatic reassessment, so quiet periods still update
waiting aircraft after movements, outage boundaries, and delayed gate entry.
Run status counts source events separately from `reassessments_delivered`.

With Julia, the `JSON`, `JuMP`, and `Gurobi` Julia packages, and a Gurobi license
already installed, run:

```sh
python3 run_local.py
```

This starts the matcher on `http://127.0.0.1:9000` and the updater on
`http://127.0.0.1:8080`, then replays the checked-in **400-flight day at 480×**.
The replay takes about three minutes. The first ordinary update arrives about
40 seconds after initialization; the initial quiet period is intentional.

The script warms the persistent Julia solver before sending the initial webhook,
then prints the run-status URL, progress, and log/record paths. After the
replay, the APIs remain available until **Ctrl-C**, which stops both servers and
the Julia worker. Existing servers are not reused or stopped; ports 8080 and
9000 must be free. Failed runs print the error and exit with a nonzero status.

Set the speed explicitly with `python3 run_local.py --speed 480`, or use
`python3 run_local.py --speed 60` for a slower replay. Speed must be finite and
greater than zero. The script finds Julia
on `PATH` or in the default Juliaup directory; `GMS_JULIA` can specify its path.
It does not install packages or change algorithm settings. The manual steps below
remain available for separate processes or another scenario.

To run the 1,200-flight day without pacing:

```sh
python3 run_local.py --scenario fixtures/pearson-2026-11-07-seed42.json --mode unpaced
```

`--scenario` selects the input file. `--mode unpaced` delivers updates as quickly
as matching acknowledges them; `--speed` only controls timing in paced mode.

## Dynamic update server

Requires **Python 3.11+**, with no third-party dependencies. Run all commands from
the repository root.

### 1. Generate a day

```sh
python3 -m smartgate_stream generate \
  --seed 42 --date 2026-11-07 --flights 1200 --rate 60 \
  --output /tmp/pearson-scenario.json
```

The output file must not already exist. Omit `--date` to use today in Toronto.
Defaults are seed `1`, `1200` flights, and rate `60`; flight counts must be 1–9999.
The flight count is the number of scheduled legs in the initial snapshot.
Generation includes the known schedule, parked aircraft, and later updates,
including early arrivals, delays, equipment changes, outages, and diversions.
Only initially parked aircraft receive initial `gate` values. Future flights do
not occupy initial gates. The `gateState` object covers terminal passenger gates
and the H remote positions included for this simulation.
The 110 positions use the explicit IFATC crosswalk and size classes. Each flight
leg includes `trafficType`: `Domestic`, `Transborder`, or `International`.
Generation reserves capacity for complete ground stays across the baseline day,
including idle aircraft and spares. Replay rejects an overcapacity baseline before
sending any webhook.

To skip generation, use the checked-in
[seeded daily scenario](fixtures/pearson-2026-11-07-seed42.json) as `scenario_path`.
The [400-flight test scenario](fixtures/pearson-400-flights-seed42.json) is smaller;
see its [usage notes](fixtures/README.md).

### 2. Start the control server

In one terminal:

```sh
python3 -m smartgate_stream serve --port 8080
```

The control API listens on `http://127.0.0.1:8080`. Leave it running; Ctrl-C stops
the server and further delivery.

### 3. Start a matcher and replay

Start your matcher or a test receiver separately. It must accept the
[webhook contract](docs/stream.md#webhook-api), including `InitializeAirport`.
The example below assumes it listens at `http://127.0.0.1:9000/updates`; replace
that URL with your receiver's address. The active matcher and algorithm are on
the `matching` branch. Follow the [connection guide](docs/matcher-integration.md)
to run that branch's receiver from a separate checkout without changing your
partner's working files.

From another terminal, start a quick replay:

```sh
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"/tmp/pearson-scenario.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"unpaced"}'
```

The `201` response includes `run_id` and `records_directory`. Replace `RUN_ID`
below with the returned ID:

```sh
curl -sS http://127.0.0.1:8080/runs/RUN_ID
curl -sS -X POST http://127.0.0.1:8080/runs/RUN_ID/stop
```

`unpaced` delivers as quickly as acknowledgments allow. Use `"mode":"paced"` to
preserve event gaps at the scenario's rate. At `60`, one real second represents
one simulated minute. Quiet periods remain quiet, including the gap after
initialization. Add `"speed":120` to the run request to override the scenario rate.

## Control API

| Method | Endpoint | Result |
| --- | --- | --- |
| POST | `/runs` | Validate a scenario and start replay; `201` with run status |
| GET | `/runs/RUN_ID` | Inspect status, counters, timing, and record location; `200` |
| POST | `/runs/RUN_ID/stop` | Stop further delivery; `202` while stopping |

`POST /runs` requires `scenario_path` and `webhook_url`. Optional `mode` defaults
to `paced`; optional `speed` overrides `metadata.rate`, which defaults to `60`
when absent. Rates must be finite and positive. Scenario paths are resolved on
the server, relative to its working directory.

Only one run may be active. Statuses are `running`, `stopping`, `completed`,
`stopped`, and `failed`. Invalid input returns `400`, unknown runs/endpoints `404`,
and overlapping starts or stopping a finished run `409`. An in-flight webhook may
finish after a stop request.

Status also includes `gate_state`, `gate_state_time`, `unassigned_aircraft`,
`planned_assignments`, `latest_decision`, and `decision_event_count`. The dashboard
can poll `GET /runs/RUN_ID/events?after=0` or subscribe to live
`GET /runs/RUN_ID/events/stream?after=0` (Server-Sent Events). Each event contains
the source event timestamp, simulated decision timestamp, actual UTC response
receipt time, matcher assignments/moves, gate changes, and resulting gate state.
Every processed webhook is represented, including events that make no gate changes.
Each run saves `scenario.json`, `deliveries.jsonl`, append-only
`decision-events.jsonl`, and the latest complete `gate-state.json` in the returned
`records_directory`. Run-status lookup lasts until the server shuts down; saved
records remain on disk.

## Matcher webhook API

The updater sends one JSON message per `POST` to the configured `webhook_url`.
The body is the message itself, without a scenario envelope. Requests include:

```http
Content-Type: application/json
X-Stream-Run-Id: <run-id>
X-Stream-Event-Id: <run-id>:<zero-based-index>
X-Stream-Simulated-Time: <UTC-announcement-timestamp>
```

Generated days start with `InitializeAirport`, containing `flights`,
`parkedAircraft`, `gateOutages`, and `gateState`. The matcher loads the supplied
initial positions and plans future assignments, then returns **200 or 204** when ready.
Returning `202` fails initialization. The simulation clock starts after readiness;
each generated day is independent.

Every request includes current `gateState` and `unassignedAircraft`. Return
synchronous `assignments` and `moves` as **200 application/json**; see the
[gate-state contract](GateStateSpecification.md) for the response format. Future
assignments become occupied at arrival time. Later updates arrive sequentially,
after the preceding decisions have been applied. Empty ordinary `2xx`
acknowledgments make no plan changes. Invalid decisions, redirects, other
statuses, network errors, or a 10-second socket timeout fail the run. There are
no retries. Evaluate schedules using the supplied simulated time.

See the [JSON Flight Message Specification](JsonFlightMessageSpecification.md)
for initialization and all eight update actions, and the
[stream guide](docs/stream.md) for complete timing, generation, and delivery rules.

## Operator view

A read-only gate timeline for operators, reading the matcher's WebSocket API. See the
[operator API and viewer](operator/SPEC.md). The passenger board is in `passenger/`
([spec](passenger/SPEC.md)).

Both views connect to the matcher at `ws://<page host>:9000/{operator,passenger}/ws` by default.
Without one they still load, say that there is no data source, and retry every 3 s.
Add `?source=mock` for simulated data with a stand-in matcher whose gate choices are potentially incorrect.

```sh
python run_local.py                               # matcher + stream on :9000/:8080
cd operator && npm install && npm run dev         # http://localhost:5173/
cd passenger && npm install && npm run dev        # http://localhost:5173/board.html (5174 if both run)
```

## Reference data

- [Pearson airport data](data/pearson.json) and [sources and usage notes](data/README.md).
- [Aircraft types, size classes, and service roles](data/aircraft.json).

## Checks

```sh
python3 -m unittest discover -s tests
```

Tests include localhost webhook integration and require permission to bind local
sockets; they do not require internet access or a running matcher.


## Matcher and dashboards

The matcher returns validated passenger-gate `assignments` and aircraft `moves` on every webhook. It serves the passenger dashboard WebSocket at `/passenger/ws` and the shared operator JSON snapshot at `/assignments`. See [Matcher API](docs/matcher-api.md) for both dashboard contracts.
