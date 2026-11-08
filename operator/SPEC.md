# Operator view

Gate-focused view for operators, showing what's happening across the airport from the top down (see the top-level `SPEC.md`).
Shows every gate and remote stand on a timeline, with the flights and parked aircraft holding them, outages, and the waitlist.
Read-only: the operator can't send decisions back. This is a known prototype limitation; a real product would send them over this view's endpoint, which is kept separate from the passenger one for that reason.

# API

The view reads from the matching algorithm's API over a single WebSocket.
The passenger view ([`../passenger/SPEC.md`](../passenger/SPEC.md)) has its own endpoint, which for now sends these same messages.
The API carries only dynamic data. Static references (the gate inventory, size classes) are imported from [`../data`](../data), as the stream and the matcher read them, never copied into messages.

## Where the data comes from

The matcher receives the stream's messages ([JSON Flight Message Specification](../JsonFlightMessageSpecification.md),
[gate-state contract](../GateStateSpecification.md)) and **passes flight data through unchanged**: everything here is the stream's
data with gate information added on top. Only these fields are the matcher's:

| Field | Source |
|-------|--------|
| `Flight.gate`, `occupancy`, `assignedAt` | The matcher's assignments and moves |
| `Flight.linkedFlightId` | Derived by the matcher (see below); the stream has no field for it |
| `Flight.previous`, `cancelled` | The matcher's record of the stream's updates |
| `Flight.misc.waitlistReason` | The matcher |
| `parkedAircraft[].gate`, `occupancy` | `InitializeAirport.parkedAircraft`, then the matcher's moves |

Everything else maps directly: `flightCodes` from `flight.carrier` + `flightNumber` + `flightSuffix` and the leg's `codeshare`;
`from`/`to` from the stations; times from the leg's local times in each station's time zone; `registration`, `aircraftType`,
`emergencyState`, `trafficType` as sent; outages as sent, with their times made absolute.
Message fields with no slot here (e.g. a diversion's `reason`) go in `misc`.
Two passenger fields repeat static data for the passenger API's sake: `airline` (from `airlineTerminals` in
[`../data/pearson.json`](../data/pearson.json)) and `Gate.terminal` (implied by the gate key: `"1"` or `"3"`, or `"Remote"` for remote stands).

## Connection

```
ws://<host>/operator/ws
```

The server sends the full state on connect, then again whenever anything changes.
Updates are batched on a one-second tick: changes within the same second are sent together as one message at the next tick.
On a tick with no changes it sends a heartbeat instead, so the view's clock keeps moving and it can tell a quiet airport from a dead connection.
There are no deltas and no sequence numbers: every full message replaces the previous one, so a reconnect or reload just receives the current state.
There are no gate selectors and no time window: every message holds everything currently decided, and the view filters by gate and time itself.

## Messages

```jsonc
{
  "type": "assignments",
  "generatedAt": "2026-11-07T13:12:44-05:00",  // the algorithm's (simulated) clock; the view's "now"
  "outages": [Outage, ...],
  "flights": [Flight, ...],
  "parkedAircraft": [ParkedAircraft, ...]
}
```

```jsonc
{ "type": "heartbeat", "generatedAt": "2026-11-07T13:12:45-05:00" }  // nothing changed; only the clock moves
```

The passenger endpoint (`ws://<host>/passenger/ws`) sends these same messages, heartbeats included.
The endpoints are separate so each can be restricted on its own later: the passenger one trimmed to passenger data, and this one the place for operator requests.

### Gate inventory (imported, not sent)

The view reads the inventory from [`../data/pearson.json`](../data/pearson.json) (`src/inventory.ts`), the same way the stream does:
one resource per key of the stream's `gateState` (`passengerStandMapping`: 78 terminal gates and 32 H positions), with its
passenger aliases (swing gates), IFATC size class, `conflictGroup` (H01, H01A and H01B can't be occupied at once) and IFATC stand names.
`Gate.gate` is that key: a passenger label such as `"D32"`, or `"Remote Stand H01A"` for remote stands (shown as "H01A"), whose `Gate.terminal` is `"Remote"`.
Aircraft size classes come from [`../data/aircraft.json`](../data/aircraft.json) by `aircraftType`.
Changing the inventory means changing the data files, as for the stream; the API has no way to add or change gates.

### Outage

A `GateOutage` as the stream announced it, with its window made absolute. The view resolves the stand through the inventory:
it closes that stand's passenger gate, and for an H position its whole conflict group.

```jsonc
{
  "stand": "Gate 132",                  // the GateOutage's `gate`: an IFATC stand name
  "start": "2026-11-07T08:00:00-05:00",
  "end": "2026-11-07T10:00:00-05:00",   // null: until further notice
  "misc": {}                            // e.g. "reason"
}
```

### Flight

Every field of the passenger `Flight`, with the same meaning, plus:

```jsonc
{
  // … passenger fields: flightId, flightCodes, airline, from, to, timeArrival, timeDeparture,
  //   gate, cancelled, previous, misc
  "registration": "C-FGJI",
  "aircraftType": "321",                  // IATA type code; its size class comes from ../data/aircraft.json
  "emergencyState": null,                 // "Turnback", "Emergency" or null
  "trafficType": "Domestic",              // "Domestic", "Transborder" or "International"
  "occupancy": {                          // the window this flight holds its gate; null without a gate
    "start": "2026-11-07T13:40:00-05:00",
    "end": "2026-11-07T14:45:00-05:00"
  },
  "assignedAt": "2026-11-07T09:02:00-05:00",  // when the current gate was assigned (simulated time); null without a gate
  "linkedFlightId": "…"                   // the other half of this aircraft's turn at YYZ; nullable
}
```

- **Arrival or departure**: a flight is an arrival when `to` is `YYZ`. A turnback is the stream's separate YYZ→YYZ `DivertFlight` (suffix `R`, `emergencyState: "Turnback"`), so it is an arrival; it doesn't change the flight that departed.
- **Waitlist**: a flight that is not cancelled and has `gate: null` is on the waitlist. Every flight is either assigned or waitlisted. The view lists every waitlisted flight it receives, earliest first, and flags arrivals already on the ground and departures past their time.
- **`occupancy`** follows the stream's physical model and is decided by the algorithm. An aircraft holds its position from arrival until its next departure, or until nightly reset if it has none (unpaired arrivals, diversions, turnbacks). Aircraft parked at initialization hold their position from the start of the day. There are no buffers. A move (tow) splits a turn: the inbound's occupancy ends at the move and the outbound's starts there. The view doesn't derive occupancy from flight times.
- **`assignedAt`**: an arrival whose gate was assigned after it arrived, with no gate before (`previous.gate` absent or null), was **brought from the waitlist**. Its waiting time is `assignedAt − timeArrival`. Example: scheduled to land 11:00, assigned at 12:00, leaves 13:30: `occupancy` is 12:00–13:30 and it spent 1 h on the waitlist. The view marks its bar with a leftward ⟵ badge and shows the wait in the flight panel.
- **`linkedFlightId`** pairs an inbound with the same registration's next non-cancelled departure from YYZ (the stream's ground visit). The matcher derives and guarantees it: the stream sends single legs with no pairing field, and an equipment change can move a departure to another aircraft. When both halves are on the same gate the view draws them as one turn.
- **Changing gates**: when the halves are on different gates (or one is waitlisted), the view marks both bars with the other half's gate.
- **`previous`** works as in the passenger API and may also hold the operator fields (e.g. a previous `aircraftType` after an equipment swap). A cancelled flight loses its gate, so `previous.gate` holds where it was.

### ParkedAircraft

An aircraft holding a position without a flight: idle aircraft and spares on the ground since initialization, and their moves.
An aircraft with a flight is shown through that flight's `occupancy` instead.

```jsonc
{
  "carrier": "AC",
  "registration": "C-FGJI",
  "aircraftType": "321",
  "gate": { "terminal": "1", "gate": "D36" },  // null: on the ground without a position
  "occupancy": { "start": "2026-11-07T00:00:00-05:00", "end": "2026-11-08T00:00:00-05:00" },
  "assignedAt": "2026-11-07T00:00:00-05:00"
}
```

# Known unknowns

- **Alarms.** Not specified, and may be cut. The view keeps an empty panel for them; they would likely arrive as another top-level array.
- Message size: a full message is ~690 KB for 1,200 flights. Full messages only go out when something changes (about a third of ticks in the mock), heartbeats otherwise. Deltas only if measured to be needed.
- **Two waitlists.** This API only has "no gate assigned" (`gate: null`). An aircraft can also be on the ground unable to enter the gate it was assigned: the matcher planned a later entry (`occupancy.start` after `timeArrival`), or the stream refused the arrival because the gate was still occupied or out of service. The stream reports both kinds together in `unassignedAircraft`, by registration. How the API distinguishes them is undecided.
- Waitlist order is the view's (earliest first). `misc.waitlistReason` is free text until the reasons are pinned down.
- Which label a swing gate is currently using (e.g. D32 or F32). Neither the stream nor this API has a field for the active mode.
- Flight status (boarding, departed, …) is not in the stream, so there is no `misc.status`.

# Viewer

TypeScript + Vite, no framework. One page, configured by URL parameters:

| Parameter  | Meaning                                                                                       |
|------------|-----------------------------------------------------------------------------------------------|
| `source`   | A WS URL (default: the matcher, `ws://<page host>:9000/operator/ws`) or `mock`                |
| `scenario` | Mock only: `1200` (default), `400` or `scripted` from [`../fixtures`](../fixtures), or the URL of a scenario file |
| `rate`     | Mock only: simulated seconds per real second (default 60)                                     |
| `start`    | Mock only: Toronto time (`HH:MM`) on the scenario's day to start at; default: the current time of day |

```
cd operator && npm install && npm run dev
```

Other scenarios come from the stream's generator (`python3 -m smartgate_stream generate`, see [`../docs/stream.md`](../docs/stream.md)).
Save one in `operator/public/` and open the view with `?scenario=/<file name>`.

## Mock data: potentially incorrect

The mock (`src/mock.ts`, `src/mock/`) stands in for the matching algorithm until it exists. Don't take its output as correct.

- **Passed through from the stream:** flights, timing changes, cancellations, equipment changes, diversions, turnbacks, outages and initial parked positions.
  The mock replays the stream's own scenario files the way the stream delivers them. Against the stream's `GateState` replay of both checked-in fixtures, every YYZ-side time, aircraft and cancellation matches, and so does the number of aircraft on the ground every 30 minutes.
- **Potentially incorrect, invented by the mock:** every gate choice, `occupancy` split, `assignedAt`, tow, the waitlist and its reasons, and the turnback/emergency preferences. They come from a greedy stand-in (`src/mock/matcher.ts`), not the real algorithm, and are not operationally validated.
  It does keep the stream's physical rules: one aircraft per conflict group, sizes, outages, and stays from arrival to departure.
- **Potentially incorrect, browser time zones:** the mock converts station-local times in the browser, so the far-end time of flights to/from Vancouver, Calgary/Edmonton and Winnipeg can be an hour off where the browser's tzdata predates 2026f. The view never displays those times; YYZ times are unaffected.
- The scenario's day is fixed (2026-11-07 for the checked-in fixtures); the mock starts at the current time of day on that date.
