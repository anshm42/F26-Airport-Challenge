# Matching test scenario

[pearson-400-flights-seed42.json](pearson-400-flights-seed42.json) is a synthetic,
seeded Pearson day for **2026-11-07**, using seed **42** and a default replay rate
of **60**. It contains **400 scheduled flight legs**, **25 initially parked
aircraft**, and **159 timestamped events**:

| Event | Count |
| --- | --- |
| InitializeAirport | 1, containing all 400 scheduled flights |
| UpdateTiming | 101, including early inbound arrivals and outbound delays |
| ReplaceFlight | 17 |
| UpdateEquipment | 10 |
| CancelFlight | 19 |
| ReinstateFlight | 8 |
| GateOutage | 3 |

There are no diversions in this sample: the current generator adds them only for
scenarios with at least 600 scheduled flights.

## Load and apply

Read `events[0].message` as the `InitializeAirport` snapshot. Its `flights` array
contains the full baseline schedule as `CreateFlight` messages; `parkedAircraft`
lists aircraft already at YYZ, and `gateOutages` is empty at startup. The parked
aircraft include twelve scheduled morning departures, three idle aircraft, and
ten spares for later equipment swaps. Each parked record includes its initial
`gate`; the full `gateState` contains those occupants and `null` at all other
positions. Future flights have no initial gate. The matcher returns future
assignments separately, and they take effect at arrival time.
The map contains 110 positions matched to IFATC and respects its size classes.
Every flight leg and reference includes `trafficType` (`Domestic`, `Transborder`,
or `International`).
The full baseline fits passenger-gate capacity throughout the day; the peak is
47 grounded aircraft, including idle aircraft and spares. Later updates can
extend stays or remove gates and require the matcher to resolve new conflicts.

After initial allocation, apply `events[1:]` in order at each event's UTC
`timestamp`. The stream adds current `gateState` and `unassignedAircraft` to each
event's `message` before delivering it as the webhook body. Later flight modifications
reference the same flight identities as the baseline; the baseline flights are
not sent again as separate creation events. Retain the initial snapshot separately
if comparing the original plan with the final result.

The file is self-contained input: reading it does not require the generator's
fleet/profile files or internet access. Load the static
[airport](../data/pearson.json) and [aircraft](../data/aircraft.json) references for
allocation. See the [message specification](../JsonFlightMessageSpecification.md)
and [webhook contract](../docs/webhook-api.md) for field and delivery rules.

## Replay through the update server

Run the update server from this `stream` checkout, then supply the scenario path
in the run request:

```sh
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_path":"fixtures/pearson-400-flights-seed42.json","webhook_url":"http://127.0.0.1:9000/updates","mode":"unpaced"}'
```

Replace the webhook URL with your matcher's address. The
matcher must finish initialization and return `200` or `204` before subsequent
updates are sent. `unpaced` runs as quickly as acknowledgments allow; use `paced`
to respect the fixture's 60× rate. Full commands are in the
[server quick-start README](https://github.com/anshm42/F26-Airport-Challenge/blob/stream/README.md#dynamic-update-server).

## Reproduce

From the `stream` branch:

```sh
python3 -m smartgate_stream generate \
  --seed 42 --date 2026-11-07 --flights 400 --rate 60 \
  --output /tmp/pearson-400-flights-seed42.json
```

The output path must not already exist. Retain this checked-in fixture for stable
comparisons; exact regeneration depends on the generator, input profiles, and
Python version.
