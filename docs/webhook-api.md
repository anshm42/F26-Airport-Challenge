# Webhook API

This contract is owned by the updater. The matching algorithm must implement a
receiver that accepts these requests and maps them into its own flight state.
The stream owns passenger-gate occupancy; the
[gate-state contract](../GateStateSpecification.md) defines the full object and
the matcher's synchronous assignment/move response.
The run's `webhook_url` supplies the receiver's HTTP(S) address and path; examples
use `POST /updates`. Changing that URL does not change the API's payload or semantics.

A generated daily replay begins with `InitializeAirport`, containing the known
schedule, initially occupied gates, and known outages. The matcher replaces its previous
dynamic state, preserves the supplied initial positions, and completes planning before returning HTTP `200` or
`204`. Returning `202` fails initialization. The replay clock starts after this
readiness response. See
[daily initialization](../JsonFlightMessageSpecification.md#daily-initialization).
For older fixtures without `InitializeAirport`, prepare the matcher before replay;
a new run ID alone does not reset it. Days remain independent.

Each event sends one `POST` with `Content-Type: application/json`, containing its
message fields plus the stream's current `gateState` and `unassignedAircraft`.
The body follows the
[JSON Flight Message Specification](../JsonFlightMessageSpecification.md); it has no
`events`, `timestamp`, or `message` wrapper. The envelope stays outside the body, in
required headers:

- `X-Stream-Run-Id`: random run identifier.
- `X-Stream-Event-Id`: `RUN_ID:INDEX`, with a zero-based file index.
- `X-Stream-Simulated-Time`: the event's exact timestamp string.

The receiver must accept `InitializeAirport` and all eight update actions, and use the supplied simulated
time for schedule evaluation. Requests arrive sequentially in fixture order,
including events with equal timestamps; the updater waits for each acknowledgment
before sending the next event. An asynchronous matcher must preserve that order
when applying updates.

Return HTTP `200` with a JSON decision object to assign future gates or move
already-grounded aircraft. Future choices do not occupy gates until arrival.
The stream validates and applies the whole response atomically. An empty ordinary
2xx acknowledgment, including legacy `202`, makes no plan changes; `204` has no
body. Initialization still requires `200` or `204`. Decision bodies are limited
to 1 MiB. Measure matcher computation in matcher logs, correlated by event ID.
Redirects, non-2xx
responses, connection failures, and a 10-second socket timeout fail the run at the
first affected event. There are no retries.

## Time and matcher responsibilities

Flight dates and times are local to their respective stations. The
`X-Stream-Simulated-Time` header is a UTC announcement timestamp, not a flight's
arrival or departure time. Use it as the simulation clock rather than wall-clock
receipt time; replay can run faster or slower than real time.

Gate-outage times are local to Toronto. For an undated outage window, use the
announcement's Toronto date; an end time earlier than the start crosses midnight.
An omitted end time means the outage continues until further notice.
`GateOutage.gate` uses exact IFATC names from the passenger crosswalk's
`ifatcStandNames`, such as `Gate 132` for the shared `D32`/`F32` passenger resource.
An outage closes the whole mapped group, including overlapping H positions.
Passenger labels are rejected in outages; gate-state keys and matcher decisions
use passenger labels.

The updater validates message structure and preserves unknown extension fields.
The stream validates known flight/gate references, passenger eligibility, and
current occupancy. The matcher owns optimization and its resource-compatibility
rules. Decisions cannot overwrite another occupant or assign excluded resources.

## Example request

From the repository root, send the [complete example body](examples/gate-state-request.json):

```sh
curl -X POST http://127.0.0.1:9000/updates \
  -H 'Content-Type: application/json' \
  -H 'X-Stream-Run-Id: example-run' \
  -H 'X-Stream-Event-Id: example-run:0' \
  -H 'X-Stream-Simulated-Time: 2026-11-07T12:30:00Z' \
  --data-binary @docs/examples/gate-state-request.json
```

The receiver can acknowledge this with `204 No Content`. See the
[JSON Flight Message Specification](../JsonFlightMessageSpecification.md) for
complete examples of initialization, all eight update actions, flight identity, update scopes,
and emergency priority rules.
