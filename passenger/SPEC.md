# Passenger gate view

Flight-focused board for passengers inside the airport (see the top-level `SPEC.md`).
Shows each flight's time and gate, and makes delays, gate changes and cancellations obvious.
Designed to run on laptops: each board is configured by its URL and splits its flights into pages that the viewer turns manually.

# API

The view reads from the matching algorithm's API over a single WebSocket.
The view doesn't need to know how assignments are produced; it only displays the latest list.

## Connection

```
ws://<host>/passenger/ws?gates=<selectors>
```

The server sends the full assignment list on connect, then again every time anything in the list changes.
There are no deltas and no sequence numbers: every message replaces the previous one, so a reconnect or reload just receives the current list.

### Gate selectors

`gates` is an optional, comma-separated list of selectors. A flight is included if any selector matches its current gate **or** its previous gate,
so a board at the old gate keeps showing "moved to …" instead of silently dropping the flight.

| Selector | Matches                               |
|----------|---------------------------------------|
| `1`      | every gate in Terminal 1              |
| `1:D`    | the D branch of Terminal 1            |
| `1:D24`  | gate D24 in Terminal 1                |
| `1:D20-D30` or `1:D20-30` | gates D20 to D30 inclusive, within one branch |

A branch is the leading letters of a gate name (`D24` → `D`).
The terminal is always required, because branch letters aren't guaranteed to be unique across terminals.
Without `gates`, every flight is included, including ones with no gate yet.

## Message

```jsonc
{
  "type": "assignments",                       // envelope, so other message types can be added later
  "generatedAt": "2026-11-07T13:12:44-05:00",
  "flights": [Flight, ...]
}
```

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
  "gate": { "terminal": "1", "gate": "D24" },  // null if not assigned yet
  "cancelled": false,
  "previous": {                                // prior values of any changed fields; any subset, may be empty
    "gate": { "terminal": "1", "gate": "D20" },
    "timeDeparture": "2026-11-07T14:05:00-05:00"
  },
  "misc": {}                                   // free-form extra information
}
```

- **`flightCodes`** is never empty. The first entry is the primary: `flight.carrier` + the leg's `flightNumber` (+ `flightSuffix`) from [`../JsonFlightMessageSpecification.md`](../JsonFlightMessageSpecification.md). The alternates follow in the order of the leg's `codeshare` array (`carrier` + `flightNumber`). Codeshares are aliases for the same leg, never separate flights or gate assignments. The view shows each code with its airline's logo and, when they don't fit, scrolls through them automatically in a continuous loop.
- **Arrival vs departure** is derived from whether `from` or `to` is `YYZ`.
- **Times** are ISO 8601 strings and must include a UTC offset. The server rejects times without one; the view never assumes local time and always formats in `America/Toronto`, regardless of the device's timezone.
- **`previous`** holds the last value of any field that changed. It may contain any top-level field except `flightId` and `previous`. The server only has to remember the last value per field, not a history. The view displays whatever is present and ignores what isn't.
- **`misc`** is for information we may or may not have access to (status, aircraft, …). The view renders keys it recognises and ignores the rest; the server can add keys without coordinating. Recognised so far: `status` (string, shown as-is).
- **Every time is an estimate** until the aircraft is at the gate. There is no tentative/confirmed distinction.

## Flight window

The algorithm only returns active assignments: everything for the day, keeping finished flights for ~1h (longer if delayed or problematic).
The view shows everything it receives, sorted by local time, split into pages. Pages only change when the viewer turns them (buttons, ←/→, Page Up/Down); updates keep the current page.

# Known unknowns

- Whether `timeArrival`/`timeDeparture` are scheduled or estimated, and whether we can get both.
- Which statuses we have access to (boarding, departed, …); until then they live in `misc`.
- When a flight is marked cancelled, and how long it stays on the board afterwards.
- Boarding time is not available. The view assumes boarding starts 30 minutes before departure, unless `misc.status` says otherwise.

# Viewer

TypeScript + Vite, no framework. Two pages:

- `/` is the setup page: pick flights, language and gates, preview the board, then open it.
- `/board.html` is the board, configured entirely by URL parameters (so a laptop can be bookmarked). Press `g` on the board to go back to setup with its current settings.

Board URL parameters:

| Parameter | Meaning                                                              |
|-----------|----------------------------------------------------------------------|
| `gates`   | Gate selectors, forwarded to the WebSocket (see above). |
| `show`    | `all` (default), `departures` or `arrivals`                          |
| `lang`    | `en` (default) or `fr`. The title stays bilingual, chosen language first |
| `source`  | `mock` (default, hard-coded data with simulated changes) or a WS URL |

Gate changes are only highlighted when the flight is within the next hour, or when it has moved off this board's gates.

```
cd passenger && npm install && npm run dev
```
