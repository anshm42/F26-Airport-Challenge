# Webhook API

This contract is owned by the updater. The matching algorithm must implement a
receiver that accepts these requests and maps them into its own flight state.
The run's `webhook_url` supplies the receiver's HTTP(S) address and path; examples
use `POST /updates`. Changing that URL does not change the API's payload or semantics.

A generated daily replay begins with `InitializeAirport`, containing the known
schedule, parked aircraft, and known outages. The matcher replaces its previous
dynamic state and completes initial allocation before returning HTTP `200` or
`204`. Returning `202` fails initialization. The replay clock starts after this
readiness response. See
[daily initialization](../JsonFlightMessageSpecification.md#daily-initialization).
For older fixtures without `InitializeAirport`, prepare the matcher before replay;
a new run ID alone does not reset it. Days remain independent.

Each event sends one `POST` with `Content-Type: application/json`, containing its
`message` unchanged in value (JSON formatting may differ). The body follows the
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

For ordinary updates, the receiver returns a 2xx status to acknowledge receipt (for example, `204 No
Content` or `202 Accepted`). The updater ignores the response body. An acknowledgment
does not prove matching computation finished. Measure
matcher computation in matcher logs, correlated by event ID. Redirects, non-2xx
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

The updater validates message structure and preserves unknown extension fields.
The matcher owns existing-flight lookup, aircraft/gate existence, resource
compatibility, occupancy, and gate/stand assignments. An acknowledgment confirms
transport receipt, not the feasibility of an assignment.

## Example request

```http
POST /updates HTTP/1.1
Content-Type: application/json
X-Stream-Run-Id: example-run
X-Stream-Event-Id: example-run:0
X-Stream-Simulated-Time: 2026-11-07T12:30:00Z

{"action":"GateOutage","gate":"D32","scheduledStartTime":"08:00","scheduledEndTime":"10:00"}
```

The receiver can acknowledge this with `204 No Content`. See the
[JSON Flight Message Specification](../JsonFlightMessageSpecification.md) for
complete examples of initialization, all eight update actions, flight identity, update scopes,
and emergency priority rules.
