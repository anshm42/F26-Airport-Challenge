# SmartGate

The dynamic update server replays synthetic Pearson flight updates to a matching
algorithm over an HTTP(S) webhook. The matcher owns flight state and gate/stand
allocation.

## Dynamic update server

Requires **Python 3.11+**, with no third-party dependencies. Use the `stream`
branch and run all commands from the repository root.

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

To skip generation, use the checked-in
[seeded daily scenario](fixtures/pearson-2026-11-07-seed42.json) as `scenario_path`.

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
that URL with your receiver's address. The updater does not provide a matcher.

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

Each run saves `scenario.json` and `deliveries.jsonl` in the returned
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
`parkedAircraft`, and `gateOutages`. The matcher replaces its previous dynamic
state and makes its initial allocation, then returns **200 or 204** when ready.
Returning `202` fails initialization. The simulation clock starts after readiness;
each generated day is independent.

Later updates arrive sequentially, with the next request sent after the previous
acknowledgment. Ordinary updates accept any `2xx` response. Redirects, other
statuses, network errors, or a 10-second socket timeout fail the run. There are
no retries. Evaluate schedules using the supplied simulated time.

See the [JSON Flight Message Specification](JsonFlightMessageSpecification.md)
for initialization and all eight update actions, and the
[stream guide](docs/stream.md) for complete timing, generation, and delivery rules.

## Reference data

- [Pearson airport data](data/pearson.json) and [sources and usage notes](data/README.md).
- [Aircraft types, size classes, and service roles](data/aircraft.json).

## Checks

```sh
python3 -m unittest discover -s tests
```

Tests include localhost webhook integration and require permission to bind local
sockets; they do not require internet access or a running matcher.
