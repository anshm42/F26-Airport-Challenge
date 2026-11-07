# SmartGate

Pearson static reference data: [dataset](data/pearson.json) and
[sources, gate zones, and usage notes](data/README.md).

Aircraft static reference data: [IATA types, size classes, and service roles](data/aircraft.json).

Updater-owned webhook API: [HTTP delivery contract](docs/stream.md#webhook-api) and
[JSON Flight Message Specification](JsonFlightMessageSpecification.md).
The matching algorithm implements the receiver against this API.

Run the dynamic update server with `python3 -m smartgate_stream serve`.
The generator creates the known schedule, parked aircraft, and subsequent updates;
the matcher makes its initial allocation before replay starts.
See [scenario generation, startup, replay controls, and the simulation clock](docs/stream.md).

Check the reference data and stream with `python3 -m unittest discover -s tests`.
