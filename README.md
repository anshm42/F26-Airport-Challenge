# SmartGate

Pearson static reference data: [dataset](data/pearson.json) and
[sources, gate zones, and usage notes](data/README.md).

Aircraft static reference data: [IATA types, size classes, and service roles](data/aircraft.json).

Updater-owned webhook API: [HTTP delivery contract](docs/webhook-api.md) and
[JSON Flight Message Specification](JsonFlightMessageSpecification.md).
The matching algorithm implements the receiver against this API.
The [gate-state contract](GateStateSpecification.md) defines current passenger
occupancy, initial gates for parked aircraft, and matcher assignment responses.

The dynamic update server and scenario generator are on the `stream` branch.
See the [quick-start README](https://github.com/anshm42/F26-Airport-Challenge/blob/stream/README.md#dynamic-update-server)
for generation, server startup, replay controls, and matcher webhook requirements.

For a small matching test, use the [400-flight scenario](fixtures/pearson-400-flights-seed42.json).
Its [usage notes](fixtures/README.md) cover loading the initial schedule, applying
updates, replay, and reproducible generation.

Check the reference data with `python3 -m unittest discover -s tests`.
