# Dynamic updates

Requires Python 3.11+ with no third-party dependencies. Run from the repository root:

```sh
python3 -m smartgate_stream serve --port 8080
```

The control API listens on `127.0.0.1` only. It delivers flight messages to a
separate matcher webhook. The stream never assigns gates, tracks matcher flight
state, or evaluates operational feasibility. `GateOutage` announces an unavailable
resource; choosing gates remains entirely the matcher's responsibility.

## Start and inspect a run

Start a matcher (or a test webhook) accepting JSON POSTs, then:

```sh
curl -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"fixtures/scripted.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"paced","speed":60}'
```

The `201` response includes `run_id`, status, counters, and `records_directory`.
Paths are resolved by the server, relative to its working directory. Fixtures are
fully loaded and validated before the worker starts. The scripted fixture covers
all eight actions, including equal-time events.

```sh
curl http://127.0.0.1:8080/runs/RUN_ID
curl -X POST http://127.0.0.1:8080/runs/RUN_ID/stop
```

`mode` defaults to `paced`; `speed` defaults to `60` and must be finite and positive.
Use `"mode":"unpaced"` to deliver as fast as acknowledgments allow. Only one run
may be active. Statuses are `running`, `stopping`, `completed`, `stopped`, and
`failed`. A stop returns `202`; stopping an already finished run returns `409`.
Invalid requests/fixtures return `400`, unknown runs/endpoints `404`, and overlapping
starts `409`. All run statuses remain available until server shutdown.

Stopping interrupts scheduled waits. An in-flight request can still affect the
matcher and must finish or time out before the run stops. A delivery error takes
precedence over a pending stop. Ctrl-C stops further delivery and closes the server.

## Fixture and webhook contract

```json
{
  "events": [
    {
      "timestamp": "2026-11-07T12:30:00Z",
      "message": {
        "action": "GateOutage",
        "gate": "D32",
        "scheduledStartTime": "08:00",
        "scheduledEndTime": "10:00"
      }
    }
  ]
}
```

`events` must be nonempty. Each timestamp is a UTC ISO 8601 datetime with seconds
and optional microseconds, ending in `Z` or `+00:00`. Decreasing timestamps are
rejected; equal timestamps preserve file order. Message validation follows the
[flight-message contract](../JsonFlightMessageSpecification.md), including scopes,
local dates/times, codeshares, and connected legs. Unknown extension fields are
preserved. Gate/aircraft existence, existing-flight lookup, and operation feasibility
belong to the matcher. Flight times are local to their stations; outage times are
local to Toronto. For undated outage windows, the matcher uses the announcement's
Toronto date; an earlier end time crosses midnight.

Each event sends one JSON POST containing its `message` unchanged in value (JSON
formatting may differ). The envelope stays outside the body, in headers:

- `X-Stream-Run-Id`: random run identifier.
- `X-Stream-Event-Id`: `RUN_ID:INDEX`, with a zero-based file index.
- `X-Stream-Simulated-Time`: the event's exact timestamp string.

The matcher must use simulated time for schedule evaluation. A 2xx response only
acknowledges receipt; it does not prove matching computation finished. Measure
matcher computation in matcher logs, correlated by event ID. Redirects, non-2xx
responses, connection failures, and a 10-second socket timeout fail the run at the
first affected event. There are no retries.

## Clock and records

The first event anchors real monotonic start time. At 60×, five simulated minutes
take five real seconds. Each paced deadline is computed from that original anchor:
`(event timestamp - first timestamp) / speed`. Requests are sequential. Late
acknowledgments cause overdue events to send immediately; deadlines never shift.
Unpaced mode advances the supplied simulated time with every event and does not
wait for deadlines.

Each run retains a unique `smartgate-stream-*` directory in the system temporary
directory, containing `scenario.json` and append-only `deliveries.jsonl`. Every
attempt records its ID, simulated time, intended deadline (seconds from run start;
null when unpaced), actual send time in UTC and seconds from start, HTTP duration,
status/error, and lateness. Records flush after each attempt. Status reports totals
for attempted/delivered messages, elapsed time, HTTP time, and lateness. Its
`simulated_time` is the last attempted event, initially null. Times describe
transport and scheduling, not matcher computation.

Remove retained directories when no longer needed. Run statuses are memory-only;
the server does not restore them after restart. A logging failure also fails the
run; the current request might already have reached the matcher.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

The suite includes localhost webhook integration tests and fake-clock checks, so
it needs permission to bind localhost sockets but no internet or real matcher.
