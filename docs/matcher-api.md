# Matcher API

The matcher is the shared source of **planned flight-to-gate assignments** for the
passenger and operator dashboards. Both dashboards read the same matcher state;
the passenger view uses a WebSocket full-list feed, while the operator view can
read the richer JSON snapshot.

Run the matcher from the repository root:

```bash
python matcher_server.py --host 0.0.0.0 --port 9000
```

The matcher receives stream webhooks at `POST /updates`. Each successful
initialization or update returns HTTP 200 with the stream decision contract:

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
  "moves": []
}
```

Gate values are canonical passenger gate IDs from
`data/pearson.json: passengerStandMapping.records`, not raw IFATC stand names.
The matcher uses each row's `conflictGroup` and maximum `widthClass`; an outage
on any exact IFATC alternative closes the entire conflict group. Aircraft types
must be both configured in `data/stream_profiles.json` and classified exactly
as `["Passenger"]` in `data/aircraft.json`. Airline terminal is a soft
preference only. `trafficType` is preserved for dashboards but is not treated
as a stand authorization matrix.

## Dashboard WebSockets

`ws://HOST:9000/operator/ws` and `ws://HOST:9000/passenger/ws` implement
`operator/SPEC.md` and `passenger/SPEC.md` on `main`: the full `assignments`
message on connect and on each one-second tick with changes, `heartbeat`
otherwise. Both currently send the same messages; the board filters by gate.
The sections below describe the older passenger message and are superseded.

## Passenger dashboard

Connect to:

```
ws://HOST:9000/passenger/ws?gates=1:D20-D30,3:A
```

The optional `gates` query parameter supports the passenger dashboard selectors:
terminal (`1`), branch (`1:D`), exact gate (`1:D24`), and inclusive branch range
(`1:D20-D30` or `1:D20-30`). A flight is included when either its current gate
or its previous gate matches, so a board at the old gate can still display a
gate-change notice.

Each connection immediately receives the complete current list. A new complete
list is sent whenever any flight record changes; no deltas or sequence numbers
are required. Passenger-facing terminal values are `1` and `3` (the underlying operator
snapshot uses `T1` and `T3`). Remote H stands use the `Remote` terminal label.

The message shape is:

```json
{
  "type": "assignments",
  "generatedAt": "2026-11-07T13:12:44-05:00",
  "flights": [
    {
      "flightId": "AC|501||2026-11-07|LEG0",
      "flightCodes": ["AC501", "UA8501"],
      "airline": "Air Canada",
      "from": "YUL",
      "to": "YYZ",
      "timeArrival": "2026-11-07T08:00:00-05:00",
      "timeDeparture": null,
      "gate": {"terminal": "1", "gate": "D22"},
      "cancelled": false,
      "previous": {},
      "misc": {
        "aircraftType": "789",
        "registration": "C-ABCDE",
        "trafficType": "Domestic"
      }
    }
  ]
}
```

Times are YYZ-local ISO 8601 values with an explicit UTC offset. The primary
operating flight code comes first, followed by codeshare aliases. `previous`
contains the prior value of fields that changed (including gate and schedule
times). Cancelled flights and completed YYZ movements remain visible for
approximately one hour. The passenger board receives full lists, not internal
solver session IDs.

Example Vite board source URL:

```
ws://localhost:9000/passenger/ws
```

Pass that URL as the passenger board's `source` parameter.

## Operator dashboard

Read the full current matcher snapshot from:

```http
GET http://HOST:9000/assignments
```

The JSON response includes:

- `assignments`: full flight-leg references and their planned passenger gates.
- `gate_assignments`: session-level reservations, including registration,
  aircraft type, planned gate, arrival/departure minutes, gate-entry minute,
  and assigned/waiting/unassigned status.
- `gate_state`: the matcher's predicted post-decision occupancy after applying
  the current response's immediate moves.
- `gate_state_before_decision`: the latest complete occupancy snapshot supplied
  by the stream before the current matcher response.
- `gate_state_source`: clarifies that the post-decision state is a matcher
  prediction; the stream's run/event feed remains authoritative after it
  validates and applies the response.
- `unassigned_aircraft` and `unassigned_details`.
- `gates`: canonical gate IDs, terminal, width class, conflict group, current
  occupant, and outage windows.
- `outages`, `solver`, `last_decision_response`, `last_decision_timestamp`,
  `run_id`, `last_event_id`, and `simulated_time`.
- `airport_timezone`, `schedule_epoch`, and `current_minute` so the operator
  view can convert session and outage minute offsets into local clock times.

The same endpoint can be polled by the operator dashboard. It does not need to
know whether an assignment came from the initial JuMP/Gurobi optimization or
the subsequent local repair.

## Health and configuration

- `GET /health`: matcher readiness, run ID, and last processed event ID.
- `GMS_JULIA`: Julia executable (default `julia`).
- `GMS_JL_BACKEND`: JuMP/Gurobi backend path (default
  `solver_backend.jl` in the repository root).
- `GMS_GUROBI_TIME_LIMIT` (default 60 s) and `GMS_GUROBI_THREADS` (default: all
  cores) for the initial solve. There is no greedy fallback: the stream retries
  `InitializeAirport` until the solve commits.
- `GMS_JL_TIMEOUT`: hard limit for one Julia request (default Gurobi limit + 60 s).
- `GMS_TOW_PENALTY` (default 2000): cost of towing an aircraft already on a gate.

The matcher only listens once Julia has started and finished a warm-up solve.

The initial allocation uses a joint JuMP/Gurobi ILP. Later stream updates retain
unaffected reservations and repair only changed, waiting, or unassigned sessions.
The ILP and repair both enforce passenger conflict groups and the same
crosswalk-based aircraft capacity rule.
