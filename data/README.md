# Pearson static reference data

[pearson.json](pearson.json) is a static research snapshot retrieved on **2026-11-07**.
It has no runtime network dependency. Every section identifies its sources; their
URLs, dates where available, and limitations are in `sources`.

It contains 54 airline terminal defaults, all 286 stand records from the supplied
IFATC page (178 named `Gate ...`), six IFATC width classes, and 89 passenger gate
groups explicitly shown on Pearson's linked departure maps. Fifteen groups have
paired swing aliases. These counts describe different source inventories and
must not be added together as the airport's gate count.

## Airline terminals

Use `airlineTerminals.records`, keyed by the two-character `carrier` code. The
three-character `icao` code is also included. Use the operating airline from
`flight.carrier` in the flight-message specification; a marketing codeshare does
not move the operating flight to another terminal.

Examples from [Pearson's directory](https://www.torontopearson.com/en/airlines):

| Terminal | Examples |
| --- | --- |
| T1 | Air Canada (AC), United (UA), Lufthansa (LH), Emirates (EK), Etihad (EY), Air India (AI), ITA (AZ) |
| T3 | WestJet (WS), Porter (PD), Air Transat (TS), American (AA), Delta (DL), British Airways (BA), SAS (SK) |

Both terminals handle domestic, US, and other international traffic. Use the
explicit airline records rather than an alliance rule: SAS moved to T3 effective
2025-10-26, as confirmed by GTAA Advisory 2025-A-041. Unlisted carriers remain
unknown. The directory does not separately resolve every operating subsidiary,
regional carrier, or brand; do not silently map QK, for example, to AC.

These are airline/check-in defaults, not exclusive gate ownership. The Infield
Concourse can serve carriers from either terminal. The 2024 IFC advisory is
historical evidence of that arrangement, not a current seasonal flight list.

## Passenger zones and swing aliases

`gateZones` describes passenger departure label conventions:

| Terminal | Label prefix | Traffic zone |
| --- | --- | --- |
| T1 | D | Domestic: Canada |
| T1 | E | International: outside Canada, excluding the US |
| T1 | F | Transborder: US |
| T3 | A | Transborder: US |
| T3 | B, C | Domestic/international areas; individual gate capability needs confirmation |

`passengerGateGroups.terminals` transcribes the labels on the official maps.
Each array is one mapped gate group. A pair such as `["D32", "F32"]` is one
resource with alternative passenger labels, not two independently available gates.
The departure maps explicitly show:

| Terminal | Swing aliases shown |
| --- | --- |
| T1 | D32/F32, D34/F34, D36/F36, D51/F51, D53/F53, D55/F55, D57/F57 |
| T1 | E66/F66, E67/F67, E68/F68, E69/F69 |
| T3 | A17/B17, A18/B18, A19/B19, A20/B20 |

D/F pairs indicate domestic/transborder switching; E/F pairs indicate
international/transborder switching. The T3 map pairs A/B labels, and GTAA's
sterility advisory specifically confirms domestic/transborder switching at
B18/B19. B/C being shared departure areas does not establish that every gate in
them supports both traffic types, or that every B/C gate is a swing gate.

An active swing mode, a sterile corridor, and any time needed to change modes are
operational state. The static list does not supply those values. Arrival customs
routing also requires separate verification.

The PDFs have **2019 filenames** and were still linked from Pearson's official
map index when checked. They are evidence for the labels shown, not proof of a
complete 2026 gate inventory. Do not treat omitted gates as nonexistent or unable
to swing. Preserve printed suffix case, including `B2a` and `B2c`.

## Stand sizes and aircraft examples

`ifatcStands.records` preserves each source `name`, `type`, and `widthClass`.
`gateWidthClasses.classes` preserves IFATC's maximum-aircraft-size key:

| Class | Aircraft examples in IFATC's key |
| --- | --- |
| A | Small general aviation aircraft, most fighters |
| B | C208, Citation X, CRJs; source also lists A-10 and P-38 |
| C | A321, 737, Dash-8, Embraers |
| D | 757, 767, MD11/DC10; source also lists C-17 and C-130 |
| E | A340, 747-400, 777, 787 |
| F | A380; source calls the other example “747-800” |

These are **simulator size hints**, not an authoritative list of aircraft accepted
at each Pearson stand. They are not a verified ICAO code-letter classification.
Generic families can hide variant differences; there is no verified mapping from
the specification's three-character aircraft codes (such as `73G`, `321`, or
`789`) to these classes. Unknown equipment compatibility must stay unknown.

Do not turn `aircraft class <= stand class` into operational approval. Dimensions,
boarding bridges, clearance from neighbours, servicing, and aircraft variants
can impose further restrictions. The source's `Airline` type also covers cargo,
remote, and maintenance positions and does not establish passenger suitability.

## Reconcile the sources before allocating gates

The IFATC table uses airside stand names and older labels, while the Pearson maps
show passenger labels. Do not join them by stripping the first digit or letter.
The GTAA signage interview documents `132` becoming `D32`, and the official map
also shows `F32`; this example is not a verified crosswalk for the entire airport.
In particular, T3 IFATC A/B/C names differ from the passenger maps.

Keep all raw stand records for traceability, but apply `knownStandExceptions`
before real-airport planning. IFATC still lists **Gate 272**, which GTAA permanently
closed during its August 2019 apron changes. Every other stand's current operating
status remains unverified; lack of an exception does not mean a stand is open.
Alternative stand positions and adjacent gates may overlap, so the 286 source
rows are not 286 independent parking resources.

The public sources consulted do not provide a complete, current GTAA matrix of
per-stand aircraft approvals, terminal/passenger-label crosswalks, customs routing,
or adjacent-position blocking rules. Those are the remaining inputs needed to
turn this reference snapshot into an operational gate-allocation inventory.

## Update and check

Refresh the static file deliberately from the linked sources, update `retrievedOn`,
and retain the distinction between source facts, map aliases, and unknowns.
Do not copy temporary closures from historical advisories into current defaults.

Run from the repository root:

```sh
python3 -m unittest discover -s tests
```

The check covers source references, source-row counts, unique stand/carrier/alias
identifiers, terminal and size-class references, representative airline defaults,
the documented swing pairs, and the closed Gate 272 exception. It does not certify
the underlying sources as current operational data.
