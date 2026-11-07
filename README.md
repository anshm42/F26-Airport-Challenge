# SmartGate

Pearson static reference data: [dataset](data/pearson.json) and
[sources, gate zones, and usage notes](data/README.md).

Aircraft static reference data: [IATA types, size classes, and service roles](data/aircraft.json).

Updater-owned webhook API: [HTTP delivery contract](docs/webhook-api.md) and
[JSON Flight Message Specification](JsonFlightMessageSpecification.md).
The matching algorithm implements the receiver against this API.

Check the reference data with `python3 -m unittest discover -s tests`.
