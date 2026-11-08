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

## Occupied gates and outages

Each request's `gateState` is the source of physical occupancy. The matcher
reserves each occupied gate for the remaining ground stay, including idle
aircraft and aircraft whose flight assignment is missing. Cancelling a
departure extends that stay to the next departure or the simulation horizon.
An equipment change changes the flight's aircraft; it does not replace the
aircraft physically parked at a gate.

An outage blocks its whole conflict group for the specified time window.
The matcher places current aircraft before future bookings. It can send an
explicit immediate move, including an atomic swap, when all destinations are
valid. Future bookings that conflict with those reservations are repaired.
If a future booking is removed or delayed, `gate: null` clears its previous
choice in the updater. The existing waiting and remote-stand rules still apply.

The matcher checks the complete reservation plan and immediate move batch
before it saves any state. If a current aircraft has no valid immediate gate,
or the batch is invalid, the request fails and the last accepted matcher state
is retained. A failed move never releases a physically occupied source gate.
The stream stops on this failure; it does not retry the request.

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
- `GMS_JL_TIMEOUT`: Python's subprocess timeout for Julia (default 9 seconds).

The initial allocation uses a joint JuMP/Gurobi ILP. Later stream updates retain
unaffected reservations and repair only changed, waiting, or unassigned sessions.
The ILP and repair both enforce passenger conflict groups and the same
crosswalk-based aircraft capacity rule.
