# Dynamic updates

Requires Python 3.11+ with no third-party dependencies. Run from the repository root:

```sh
python3 -m smartgate_stream serve --port 8080
```

The control API listens on `127.0.0.1` only. It delivers flight messages to a
separate matcher webhook. The stream never assigns gates, tracks matcher flight
state, or evaluates operational feasibility. `GateOutage` announces an unavailable
resource; choosing gates remains entirely the matcher's responsibility.

## Generate a scenario

```sh
python3 -m smartgate_stream generate --seed 42 --date 2026-11-07 \
  --flights 1200 --output /tmp/pearson-scenario.json
```

The output path must not already exist. The seed defaults to `1`, and flight count
defaults to `1200` (input range: 1–9999). The date is a Toronto local calendar
date. The same inputs and checked-in profiles reproduce the same scenario on the
same Python version. Generate once and retain that fixture for comparisons across
Python versions.

The count specifies single-leg `CreateFlight` messages. The generator adds timing
changes, replacement schedules, equipment swaps, cancellations/reinstatements,
gate outages, and occasional diversions. A 1,200-flight scenario has 600 paired
arrivals/departures, plus those extra events. Odd counts leave one unpaired inbound.
Creates announce the initial schedule during the first three hours of the day;
perturbations announce later, before affected departures. Some long-haul inbound
legs depart their origin on the preceding local date.

[Traffic profiles](../data/stream_profiles.json) use a weighted selection of twelve
Pearson-serving carriers, with actual registration/type pairs from the
[OpenSky August 2025 subset](../data/opensky_fleet_2025.json).
Route groups restrict equipment: Porter uses E195-E2s, regional routes include
Embraer regional jets and CRJs, transatlantic flights use appropriate longer-range equipment,
and Emirates' daily Dubai rotation uses an A380. Pairs reuse a tail with a plausible
turnaround, and equipment updates select another available aircraft from that
carrier's snapshot pool. Aircraft can fly multiple rotations on the same route,
with a 45-minute remote turnaround and delay headroom reserved between uses.
When a carrier's pool cannot fit another rotation, generation tries other
Pearson carriers; if no placement is found, it fails with a capacity error rather
than inventing registrations. High flight counts may exceed this historical pool.
Local station times use IANA time zones; event announcements are UTC, including on
Toronto daylight-saving transitions. No flight contains a gate assignment.

This is synthetic traffic inspired by the Pearson arrival/departure boards.
Registrations and aircraft types retain their association from the 2025 snapshot;
historical metadata may be incomplete or stale. Weights, block times, flight
numbers, codeshares, and frequency caps are simulation choices rather
than an exact daily timetable. The twelve-carrier sample is not a complete inventory
of every airline serving Pearson. See the profile source links for provenance.

Use the generated file as `scenario_path` in the run request below. Replay itself
does not contact fleet APIs or need internet access.

### Refresh the fleet subset

Download `aircraft-database-complete-2025-08.csv` from
[OpenSky's metadata snapshots](https://opensky-network.org/datasets/#metadata/), then:

```sh
python3 -m smartgate_stream.fleet /tmp/aircraft-database-complete-2025-08.csv \
  /tmp/opensky-fleet.json
```

Compare the generated subset with the checked-in file before replacing it. The
importer uses the source's single-quoted CSV format and records a SHA-256 of the
complete source. It filters explicit operator ICAO codes and compatible IATA
codes, national registration formats, configured passenger types, known expired
registrations, and inactive status values. Conflicting records for one tail are
excluded. Missing IATA codes are resolved by explicit operator ICAO, without
guessing from the owner name. Porter operators `POE`/`PTR` are explicitly grouped
under public code `PD`; original operator codes stay in each record. ICAO aircraft
type codes are mapped to the existing three-character IATA contract.

OpenSky labels this metadata as outdated and provides no current fleet guarantee.
The subset establishes a reproducible 2025 simulation input, not current ownership
or airworthiness. Other snapshot months need deliberate review of the importer
source metadata and profiles.

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
