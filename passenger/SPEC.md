# Passenger gate view

Flight-focused board for passengers inside the airport (see the top-level `SPEC.md`).
Shows each flight's time and gate, and makes delays, gate changes and cancellations obvious.
Designed to run on laptops: each board is configured by its URL and splits its flights into pages that the viewer turns manually.

# API

The view reads from the matching algorithm's API over a single WebSocket.
The view doesn't need to know how assignments are produced; it only displays the latest list.

## Connection

```
ws://<host>/passenger/ws
```

This endpoint is separate from the operator's so it can later be restricted to passenger data. For now it sends the operator's messages; see [`../operator/SPEC.md`](../operator/SPEC.md).
There are no query parameters: the server sends every flight currently decided, and the view filters.
The server sends the full list on connect, then again whenever anything changes.
Updates are batched on a one-second tick: changes within the same second are sent together as one message at the next tick.
On a tick with no changes it sends a heartbeat instead.
There are no deltas and no sequence numbers: every `assignments` message replaces the previous one, so a reconnect or reload just receives the current list.

## Messages

The `assignments` message is the operator's (see [`../operator/SPEC.md`](../operator/SPEC.md)), which is a superset of what this view needs.
The view reads the fields below and ignores the rest (`outages`, `parkedAircraft`, and the operator-only `Flight` fields), so it keeps working if they're trimmed.

```jsonc
{
  "type": "assignments",
  "generatedAt": "2026-11-07T13:12:44-05:00",  // the server's clock; the view's "now"
  "flights": [Flight, ...]
  // … operator fields, ignored
}
```

```jsonc
{ "type": "heartbeat", "generatedAt": "2026-11-07T13:12:45-05:00" }  // nothing changed; only the clock moves
```

On a heartbeat the view keeps the last flight list and only updates its clock ("Updated …") and the flight window.

### Flight

```jsonc
{
  "flightId": "…",                             // opaque, owned by the algorithm; the only unique key
  "flightCodes": ["AC101", "UA8001", "NH6812"],// primary (operating) code first, then codeshare alternates; display only, not unique
  "airline": "Air Canada",
  "from": "YYZ",                               // IATA
  "to": "YVR",                                 // IATA
  "timeArrival": "2026-11-07T19:45:00-05:00",  // nullable
  "timeDeparture": "2026-11-07T14:30:00-05:00",// nullable
  "gate": { "terminal": "1", "gate": "D24" },  // null if not assigned yet; see Gate
  "cancelled": false,
  "previous": {                                // prior values of any changed fields; any subset, may be empty
    "gate": { "terminal": "1", "gate": "D20" },
    "timeDeparture": "2026-11-07T14:05:00-05:00"
  },
  "misc": {}                                   // free-form extra information
  // … operator fields, ignored
}
```

- **`flightCodes`** is never empty. The first entry is the primary: `flight.carrier` + the leg's `flightNumber` (+ `flightSuffix`) from [`../JsonFlightMessageSpecification.md`](../JsonFlightMessageSpecification.md). The alternates follow in the order of the leg's `codeshare` array (`carrier` + `flightNumber`). Codeshares are aliases for the same leg, never separate flights or gate assignments. The view shows each code with its airline's logo and, when they don't fit, scrolls through them automatically in a continuous loop.
- **`airline`** is the airline's name, from `airlineTerminals` in [`../data/pearson.json`](../data/pearson.json) (as in the operator API).
- **Arrival vs departure** is derived from whether `from` or `to` is `YYZ`.
- **Times** are ISO 8601 strings and must include a UTC offset. The server rejects times without one; the view never assumes local time and always formats in `America/Toronto`, regardless of the device's timezone.
- **`previous`** holds the last value of any field that changed. It may contain any top-level field except `flightId` and `previous`. The server only has to remember the last value per field, not a history. The view displays whatever is present and ignores what isn't.
- **`misc`** is for information we may or may not have access to (status, aircraft, …). The view renders keys it recognises and ignores the rest; the server can add keys without coordinating. Recognised so far: `status` (string, shown as-is).
- **Every time is an estimate** until the aircraft is at the gate. There is no tentative/confirmed distinction.

### Gate

```jsonc
{ "terminal": "1", "gate": "D24" }                        // terminal gate: terminal number and passenger label
{ "terminal": "Remote", "gate": "Remote Stand H01A" }     // remote stand: the IFATC stand name is the key
```

`gate` is the gate's key, as in the operator API. The view shows remote stands without the prefix (`"Remote Stand H01A"` → `H01A`),
in the gate column and in gate-change text.

## Flight window

The server sends everything currently decided, with no time window.
The view hides a flight once its current YYZ time (`timeDeparture` for departures, `timeArrival` for arrivals; not the `previous` one, so a delayed flight stays until its new time)
is more than 1 h before the server's clock (the latest `generatedAt`, from either message type). Flights with no time stay.
The window is applied as the clock moves, so flights leave the board on a heartbeat without a new `assignments` message.
The rest is sorted by local time and split into pages. Pages only change when the viewer turns them (buttons, ←/→, Page Up/Down); updates keep the current page.

## Gate selectors

The server doesn't filter by gate; the board does, with the `gates` URL parameter (see Viewer).
`gates` is an optional, comma-separated list of selectors. A flight is shown if any selector matches its current gate **or** its previous gate,
so a board at the old gate keeps showing "moved to …" instead of silently dropping the flight.

| Selector | Matches                               |
|----------|---------------------------------------|
| `1`      | every gate in Terminal 1              |
| `1:D`    | the D branch of Terminal 1            |
| `1:D24`  | gate D24 in Terminal 1                |
| `1:D20-D30` or `1:D20-30` | gates D20 to D30 inclusive, within one branch |
| `Remote` or `R` | every remote stand             |
| `Remote:H` or `R:H` | the H remote stands        |
| `Remote:H01A` or `R:H01A` | remote stand H01A    |
| `Remote:H01-H05` or `R:H01-05` | remote stands H01 to H05 inclusive (by number, so H01A and H01B are included) |

Selectors match a gate's label: the gate name, or a remote stand's name without the `Remote Stand ` prefix.
A branch is the leading letters of a label (`D24` → `D`, `H01A` → `H`).
The terminal is always required, because branch letters aren't guaranteed to be unique across terminals.
Numeric terminals must match exactly; a named terminal also matches its first letter (`R` for `Remote`), case-insensitively, as in the operator view.
Without `gates`, every flight is shown, including ones with no gate yet.

# Known unknowns

- Whether `timeArrival`/`timeDeparture` are scheduled or estimated, and whether we can get both.
- Which statuses we have access to (boarding, departed, …); until then they live in `misc`. The stream has no flight status (see the operator spec),
  so in practice `misc.status` is absent and the board falls back to the 30-minute boarding estimate.
- When a flight is marked cancelled, and how long it stays on the board afterwards.
- Boarding time is not available. The view assumes boarding starts 30 minutes before departure, unless `misc.status` says otherwise.

# Viewer

TypeScript + Vite, no framework. Two pages:

- `/` is the setup page: pick flights, language and gates, preview the board, then open it.
- `/board.html` is the board, configured entirely by URL parameters (so a laptop can be bookmarked). Press `g` on the board to go back to setup with its current settings.

Board URL parameters:

| Parameter | Meaning                                                              |
|-----------|----------------------------------------------------------------------|
| `gates`   | Gate selectors, applied by the board (see Gate selectors); not sent to the server |
| `show`    | `all` (default), `departures` or `arrivals`                          |
| `lang`    | `en` (default) or `fr`. The title stays bilingual, chosen language first |
| `source`  | `mock` (default, hard-coded data with simulated changes) or a WS URL |

Gate changes are only highlighted when the flight is within the next hour, or when it has moved off this board's gates.

```
cd passenger && npm install && npm run dev
```
