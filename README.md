# SmartGate

Pearson static reference data: [dataset](data/pearson.json) and
[sources, gate zones, and usage notes](data/README.md).

Aircraft static reference data: [IATA types, size classes, and service roles](data/aircraft.json).

Flight-message format: [JSON Flight Message Specification](JsonFlightMessageSpecification.md).

Run the dynamic update server with `python3 -m smartgate_stream serve`.
See [scenario generation, replay controls, and the simulation clock](docs/stream.md).

Check the reference data and stream with `python3 -m unittest discover -s tests`.
