# Dynamic updates

Requires Python 3.11+ with no third-party dependencies. Run from the repository root:

```sh
python3 -m smartgate_stream serve --port 8080
```

The control API listens on `127.0.0.1` only. The updater owns the
[webhook API](#webhook-api); the matcher implements its receiving endpoint. The
stream owns current passenger occupancy and simulates aircraft movements. It
supplies initial positions for aircraft already at YYZ; the matcher chooses future
assignments and explicit moves. See the [gate-state contract](../GateStateSpecification.md).

## Generate a scenario

```sh
python3 -m smartgate_stream generate --seed 42 \
  --flights 1200 --rate 60 --output /tmp/pearson-scenario.json
```

The output path must not already exist. The seed defaults to `1`, and flight count
defaults to `1200` (input range: 1–9999). The date defaults to today in Toronto.
Use `--date YYYY-MM-DD` to select a specific Toronto local calendar date.
The same inputs and checked-in profiles reproduce the same scenario on the
same Python version. Generate once and retain that fixture for comparisons across
Python versions.

`--rate` sets the default simulation speed for paced replay (default `60`). It
must be finite and positive; fractional multipliers such as `0.5` are supported.
At `60`, one real second represents one simulated minute. Generation writes the
value to `metadata.rate`; it preserves event timestamps and flight schedules.
Replay scales the actual gaps between events, so quiet periods, irregular updates,
and bursts remain intact. There is no fixed update heartbeat.

Each generated scenario starts at Toronto midnight with one `InitializeAirport`
message containing the entire known schedule and aircraft already parked at YYZ.
No hand-written startup file is needed. The count specifies the number of
single-leg `CreateFlight` messages inside that snapshot, not additional update
events. The default 1,200-flight scenario has twelve morning departures by parked
aircraft and 594 paired arrivals/departures. Four additional parked aircraft have
no departure that day. Smaller scenarios scale these startup counts down; the
maximum remains twelve departing and four idle aircraft. These aircraft use
Air Canada's checked-in fleet and route profile. Spare aircraft needed for later
equipment swaps also start parked; they come from each affected carrier's fleet
and are reserved exclusively for that swap. Every initially parked aircraft has a
`gate`; the initial `gateState` contains those occupants and `null` at all other
positions. Aircraft scheduled to arrive later have no initial gate. Placements
use IFATC capacity classes and the carrier's terminal defaults, then compatible H
parking if needed. The explicit crosswalk contains 78 terminal groups and 32 H
positions; 11 unresolved passenger labels have been removed. These are simulation
placements, not a verified operational aircraft/stand capability matrix.

The generator reserves a compatible position for each entire baseline ground
stay, counting swing aliases and overlapping H positions once. The 110 gate-state
entries represent 92 independent resources before size and outage restrictions.
Initially parked aircraft occupy their position until their baseline departure;
idle aircraft and spares reserve it for the day. An unpaired inbound stays until
nightly reset. Arrival/departure times are resampled when capacity is unavailable;
generation fails if fleet/gate capacity cannot fit the requested count. A spare
for an equipment update is included only when a compatible position remains free
for its whole baseline day. These private reservations are feasibility proof;
the matcher still chooses all future assignments.

The generator then sends timing changes, replacement schedules, equipment swaps,
cancellations/reinstatements, gate outages, and occasional diversions, including
turnbacks. Flights in the snapshot are not sent again as individual `CreateFlight`
events. An odd remaining flight count leaves one unpaired inbound. Startup
departures leave between 06:00 and 09:59 and are held fixed; their aircraft are
excluded from other generated rotations, and idle startup aircraft stay parked.
Regular outbound perturbations announce before affected departures. Roughly a quarter
of inbound flights receive an `UpdateTiming` moving arrival 1–30 minutes earlier,
announced 5–20 minutes before the revised arrival while the aircraft is in flight.
The departure time, aircraft, flight identity, and paired outbound schedule stay
unchanged. These early arrivals can extend occupancy into a previous assignment's
window; the matcher checks feasibility and chooses any necessary reassignment.
Some long-haul inbound legs depart their origin on the preceding local date.
Announcements are sampled at second resolution within their notice windows.
Related messages can arrive together, and gaps vary between updates; flight and
outage schedule fields retain their required `HH:MM` format.

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
Every generated flight leg and reference includes `trafficType`, based on explicit
station `countryCode` values in the profiles: Canada/Canada is `Domestic`,
Canada/US is `Transborder` in either direction, and Canada/Europe, Canada/Mexico,
and other cross-border pairs are `International`. Complete incoming leg objects
require this field. The stream fills it from the known schedule when an incoming
leg reference omits it. An explicit turnback retains the intended flight category;
generated turnbacks use domestic service profiles.

Local station times use IANA time zones; event announcements are UTC, including on
Toronto daylight-saving transitions. Scheduled flight/leg objects contain no
initial gate; only the `parkedAircraft` records do. Equipment swaps use the tail's
last rotation; an earlier cancelled departure is reinstated before departure so
the same aircraft can perform its later rotations.

Diversions carry the optional top-level `emergencyState` field: `"Turnback"` gets
terminal-gate priority, while `"Emergency"` allows compatible remote stands. `reason`
is separate explanatory text. Generated turnbacks depart and return to YYZ and carry
the suffix `R`. The [flight-message contract](../JsonFlightMessageSpecification.md#gate-priority-and-remote-stands)
retains the legacy reason-based policy when the field is omitted. The stream validates
and preserves the state; the matcher applies the policy and checks resource feasibility.

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

A scenario may start with `InitializeAirport`, containing its known schedule,
initially occupied gates, and active outages. The matcher loads those positions
and plans future assignments before returning HTTP `200` or `204`; replay starts
only after that response. HTTP `202` fails initialization. See
[daily initialization](../JsonFlightMessageSpecification.md#daily-initialization).
Older initialized fixtures without gates for parked aircraft must be regenerated.
Before accepting an initialized run, the server checks baseline demand at every
movement and known-outage boundary, including after the last announcement. An
overcapacity baseline returns `400` without contacting the matcher. Later
announced disruptions can still create conflicts requiring new matching decisions.
Incremental fixtures without this message start with an empty gate map; scheduled
flights alone never imply that an aircraft is already on the ground. Days remain independent.

Start a matcher (or a test receiver) implementing the updater's
[webhook API](#webhook-api), then:

```sh
curl -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"/tmp/pearson-scenario.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"paced"}'
```

The `201` response includes `run_id`, status, counters, and `records_directory`.
Paths are resolved by the server, relative to its working directory. Fixtures are
fully loaded and validated before the worker starts. Use `fixtures/scripted.json`
as the path for a small fixed scenario covering all eight actions, equal-time events,
a medical diversion, and a turnback.

```sh
curl http://127.0.0.1:8080/runs/RUN_ID
curl -X POST http://127.0.0.1:8080/runs/RUN_ID/stop
```

`mode` defaults to `paced`. An explicit request `speed` overrides the fixture's
`metadata.rate`; when both are omitted, replay uses `60`. To use the generated
`--rate`, omit `speed` from the run request. All rates must be finite and positive.
Use `"mode":"unpaced"` to deliver as fast as acknowledgments allow. Only one run
may be active. Statuses are `running`, `stopping`, `completed`, `stopped`, and
`failed`. A stop returns `202`; stopping an already finished run returns `409`.
Invalid requests/fixtures return `400`, unknown runs/endpoints `404`, and overlapping
starts `409`. All run statuses remain available until server shutdown.

Stopping interrupts scheduled waits. An in-flight request can still affect the
matcher and must finish or time out before the run stops. A delivery error takes
precedence over a pending stop. Ctrl-C stops further delivery and closes the server.

## Scenario fixture

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
preserved. The stream validates known passenger gates, aircraft eligibility,
flight references, and physical occupancy; the matcher plans future availability
and applies its other allocation rules. Flight times are local to their stations; outage times are
local to Toronto. For undated outage windows, the matcher uses the announcement's
Toronto date; an earlier end time crosses midnight.

## Webhook API

See the [shared HTTP contract](webhook-api.md) and
[gate-state/decision specification](../GateStateSpecification.md). Each request
includes current `gateState` and `unassignedAircraft`; HTTP `200` JSON responses
can return `assignments` and `moves`. Future assignments take effect at arrival,
not when the matcher returns its choice. Empty acknowledgments make no plan changes.

## Clock and records

When the first event is `InitializeAirport`, its successful readiness response
anchors the simulation clock. Otherwise the first event's send time is the anchor.
At 60×, five simulated minutes
take five real seconds. Each paced deadline is computed from that original anchor:
`(event timestamp - first timestamp) / speed`. Requests are sequential. Late
acknowledgments cause overdue events to send immediately; deadlines never shift.
Unpaced mode advances the supplied simulated time with every event and does not
wait for deadlines. Physical arrivals and departures also advance occupancy between
announcements, without sending additional webhook messages. Initialized runs
process remaining known movements before the next Toronto midnight, even after
the final announcement; legacy incremental runs end at their last announcement.

Each run retains a unique `smartgate-stream-*` directory in the system temporary
directory, containing `scenario.json`, append-only `deliveries.jsonl`, and the
atomically replaced current `gate-state.json`. Every
attempt records its ID, simulated time, intended deadline (seconds from run start;
null when unpaced), actual send time in UTC and seconds from start, HTTP duration,
status/error, parsed matcher response, and lateness. Records flush after each attempt. Status reports totals
for attempted/delivered messages, elapsed time, HTTP time, and lateness. Its
`simulated_time` is the last attempted announcement, initially null. The separate
`gate_state_time` tracks physical movement processing and can advance after the
last announcement. Status also exposes `gate_state`, `unassigned_aircraft`, and
`planned_assignments`. HTTP duration includes the receiver's synchronous response
time; it does not measure matcher computation separately.

Remove retained directories when no longer needed. Run statuses are memory-only;
the server does not restore them after restart. A logging failure also fails the
run; the current request might already have reached the matcher.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

The suite includes localhost webhook integration tests and fake-clock checks, so
it needs permission to bind localhost sockets but no internet or real matcher.
