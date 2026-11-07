# Passenger Gate State and Matcher Decisions

## Ownership and scope

The dynamic update stream owns current passenger-gate occupancy. The matcher owns
the choice of gates and returns decisions to the stream. The stream validates and
applies those decisions before the next request. Both services use simulated time.

The tracked inventory is `passengerStandMapping.records` in
[data/pearson.json](data/pearson.json): **78 terminal gate groups and 32 H remote
positions, for 110 keys**. Each terminal gate uses the first passenger label from
its `passengerGateGroups` group. `D32` and `F32`, for example, normalize to the
single key `D32` and map to IFATC `Gate 132`. Swing aliases always share one
occupant. Use exact `Remote Stand H...` names for H positions, included as
passenger parking for this simulation. Cargo, maintenance, and GA positions are
outside this inventory.

The passenger map's `D30`, `F82`, `F83`, `E82`, `E85`, `B2a`, `B2c`, `B3`, `B4`,
`B5`, and `B23` have no unambiguous IFATC match. They are removed from the simulation
inventory and rejected in assignments, moves, initial parking, and outages.
The raw map transcription remains in the reference data for provenance.

H positions sharing the same base number are mutually exclusive: occupying
`Remote Stand H01A` blocks `Remote Stand H01` and `Remote Stand H01B`, even though
their values remain `null`. This conservative simulation rule avoids assuming an
unverified simultaneous-use matrix. A and B subpositions are not independent
capacity in this version.

The 110 entries represent **92 independent conflict groups** under this rule.
Usable capacity also depends on aircraft size and known outages; counting `null`
entries alone does not establish available capacity.

Occupants must be commercial passenger aircraft types configured in the stream's
[traffic profiles](data/stream_profiles.json),
with exactly `serviceTypes: ["Passenger"]` in
[data/aircraft.json](data/aircraft.json). Cargo, combi, special-purpose, mixed-role,
unknown, and GA/business types outside those profiles cannot occupy this object.
The generator excludes them; invalid occupancy or matcher decisions are rejected.

## Gate object

`gateState` is a JSON object containing every canonical passenger-gate ID exactly
once. Each value is either JSON `null` or an aircraft object containing exactly
`carrier`, `registration`, and `aircraftType`. A registration may occupy at most
one resource. `null` means unoccupied; outages or other constraints can still make
an empty gate unavailable.

Excerpt showing the value format (real requests contain the full inventory):

```json
{
  "D20": {"carrier": "AC", "registration": "C-FFGZ", "aircraftType": "321"},
  "D22": null,
  "D32": null
}
```

See the [complete request example](docs/examples/gate-state-request.json) for all
110 entries. Aircraft already at YYZ but waiting for a gate appear in the
request's `unassignedAircraft` array using the same three aircraft fields. They
do not occupy an invented gate or displace an existing occupant.

## Initial schedule

`InitializeAirport.parkedAircraft` supplies a required `gate` for **every aircraft
already on the ground** at the snapshot time, including idle aircraft and spares:

```json
{
  "carrier": "AC",
  "registration": "C-FFGZ",
  "aircraftType": "321",
  "gate": "D20"
}
```

Only these aircraft occupy the initial gate map. Flights scheduled to arrive
later receive no initial gate and occupy no initial resource. The `flights` array
still contains the entire known schedule, but its flight/leg objects do not carry
gate assignments. If the initialization includes `gateState`, it must exactly
match `parkedAircraft` and contain all other gates as `null`.

The initial known schedule must fit passenger parking **throughout the simulated
day**, including idle aircraft, spares, every arrival-to-departure stay, and
unpaired arrivals remaining until nightly reset. Apply IFATC size limits, shared
swing/H resources, and outages already known in the snapshot. The generator
reserves compatible positions for complete stays internally; those future choices
are not transmitted as initial gate assignments. It resamples flights when no
position fits and fails generation if capacity cannot be satisfied.

Before starting replay, check baseline demand at every movement and known-outage
boundary. An overcapacity baseline returns HTTP `400` from `POST /runs` before
any webhook is sent. This is a guarantee about the initial schedule. Later
announced early arrivals, delays, cancellations, and outages can create conflicts
for the matcher to resolve.

The matcher loads the supplied positions as current occupancy and plans the known
flights. It does not choose replacement initial positions implicitly. It can
return explicit moves before acknowledging readiness with `200` or `204`.
Initialization failure must not activate partial state. Each new initialized day
replaces the previous day's state; historical records remain available for audit.

Initial positions are synthetic placements constrained by the explicit IFATC
crosswalk. `ifatcStandNames` lists alternative stand positions for a passenger
group, and `widthClass` is their largest IFATC capacity. A single group still
holds at most one aircraft; the alternatives are not additional slots. Compare
known aircraft/stand classes in ascending A–F order. Unknown or oversized aircraft
are rejected for initial parking, assignments, and immediate moves. Recheck at
arrival after equipment changes; an incompatible aircraft waits unassigned.

`conflictGroup` identifies positions that cannot be occupied simultaneously.
The map comparisons are an editorial simulation crosswalk, not GTAA operational
approvals. The matcher remains responsible for traffic-zone capability, sterility,
bridge suitability, and other operational constraints.

## Requests to the matcher

Continue posting each flight/resource message to the configured `webhook_url`,
with the existing `action` and fields at the top level. The stream adds:

- `gateState`: the complete current occupancy object.
- `unassignedAircraft`: aircraft currently on the ground without a passenger gate.

There is no new envelope around the flight message. The existing
`X-Stream-Run-Id`, `X-Stream-Event-Id`, and `X-Stream-Simulated-Time` headers remain
required. Occupancy is evaluated at the supplied simulated timestamp. Future
updates remain private to the updater until their announcement times.

Before sending a request, advance known arrivals/departures to its timestamp and
apply the announced schedule change. A late timing revision can make a movement
due immediately. Flight times use their station's IANA time zone; movement and
announcement comparisons use UTC. Departures free resources before arrivals at
the same timestamp. Cancelling a departure does not remove its parked aircraft.

## Matcher response

Return synchronous decisions as HTTP `200` with `Content-Type: application/json`:

```json
{
  "assignments": [
    {
      "carrier": "AC",
      "leg": {
        "flightNumber": "501",
        "flightSuffix": null,
        "originatingDate": "2026-11-07",
        "departureStation": "YUL",
        "arrivalStation": "YYZ"
      },
      "gate": "D22"
    }
  ],
  "moves": [
    {"registration": "C-FFGZ", "gate": "D31"}
  ]
}
```

Both arrays are optional; an empty object or an empty acknowledgment leaves the
plan unchanged. `204` has no response body. Ordinary legacy `2xx` acknowledgments
without decisions are still accepted. Initialization accepts only `200` or `204`;
`202` is not a readiness response. Asynchronous decision callbacks are not part
of this contract.

`assignments` references an existing flight leg using its operating carrier,
flight number, optional suffix, local departure date, and station pair. Aircraft
registration is not its identity: an equipment change can replace the aircraft.
A later assignment for that leg replaces its previous plan. A `null` gate clears
the plan without making a physically parked aircraft disappear.

**A future assignment does not occupy a gate.** It takes effect at arrival time.
An outbound assignment for an aircraft arriving beforehand can supply the gate
for that ground visit when no inbound gate has been chosen. A decision concerning
the aircraft's current ground visit can move it now; a decision for a later visit
cannot move the aircraft prematurely. Arrival/departure choices for one ground
visit must be physically consistent; use an explicit move for a change while
the aircraft is already on the ground.

`moves` repositions an aircraft already on the ground, including an idle or
waiting aircraft, at the current simulated time. Destinations must be tracked
passenger gates. Moves are instantaneous in this simulation; tow duration is not
modeled. A move cannot introduce an unknown aircraft or delete one.

Validate the entire response before changing any occupancy or plan. Reject unknown
fields, malformed references, duplicate/conflicting decisions, unknown gates,
excluded or oversized aircraft, and occupied/unavailable immediate destinations. Atomic swaps
are allowed when every aircraft being moved vacates its source in the same batch.
On failure, retain the last valid state, fail the run, and send no further updates.
Responses are limited to 1 MiB and the existing 10-second socket timeout. There
are no retries; event IDs correlate decisions and delivery records.

## Movement and observation

The stream retains future assignments separately and simulates movements from
the known schedule. An arrival occupies its chosen gate only when that gate is
free and not out of service at arrival time. Otherwise the aircraft waits in
`unassignedAircraft`; an existing occupant is never overwritten. Departure clears
the aircraft's current position. Timing, cancellation, reinstatement, replacement,
and equipment updates revise future movements without resurrecting completed ones.

Internal movement ticks do not invent extra flight-update messages. Generated
initialized runs continue through their remaining known movements for the day,
even after the last announcement. The simulation uses independent Toronto days.

`GET /runs/RUN_ID` exposes `gate_state`, `gate_state_time`, `unassigned_aircraft`,
and `planned_assignments` alongside transport status. Each run's
`records_directory/gate-state.json` stores the latest complete gate object.
Validated matcher responses are recorded with their delivery attempts. The
original scenario remains separate from the evolving current state.

## Compatibility

The wire request now contains occupancy and the updater consumes decision bodies;
receivers that ignored these fields or returned only acknowledgments must implement
the response above to affect the plan. New scenarios require initial gates for
parked aircraft. Regenerate older initialized fixtures that omit them. Incremental
fixtures without `InitializeAirport` start with an empty gate map; a future
`CreateFlight` alone never establishes an aircraft's current physical location.
