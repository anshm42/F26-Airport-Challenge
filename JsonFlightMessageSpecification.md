# JSON Flight Message Specification

## Overview

This document defines the updater-owned JSON payload contract for flight schedule
updates. The matching algorithm must consume this format. The updater defines the
API independently of the matcher's internal data model; any mapping into that model
belongs in the matcher. See the [webhook API](docs/stream.md#webhook-api) for HTTP
delivery, required headers, ordering, and acknowledgments.

The `flight` object holds the shared aircraft details: operating carrier, registration, and aircraft type. The `legs` array holds the details that can change between flights: flight number, suffix, departure date, codeshares, stations, and times. All legs grouped under one `flight` use the same aircraft.

## Daily Initialization

Each day is an independent simulation. During modeled overnight downtime, the
matcher is reinitialized before the next day's replay. Its initial state consists
of the static gate, stand, and aircraft reference data, with an empty flight
schedule and no dynamic aircraft occupancy or gate/stand assignments.

The reset clears the previous day's flights, timing/equipment changes,
cancellations, and dynamic gate outages. Static restrictions remain in effect,
and prior run records are retained for audit. Aircraft and assignments are not
carried between days.

`CreateFlight` messages establish that day's baseline schedule. Subsequent update
messages refer to flights already created; `DivertFlight` can introduce an
unscheduled inbound. The generated baseline is announced during the first three
simulated hours of the day.

The matcher or test harness performs initialization before starting delivery.
Starting an updater run does not reset the receiver, and `X-Stream-Run-Id` is a
correlation identifier, not a reset instruction. Overnight downtime is a simulation
assumption; it does not represent an airport-wide closure at Pearson.

## Key Industry Terms

- **Carrier**: An airline identified by a two-character code (e.g., "AC" for Air Canada, "AA" for American Airlines).

- **Station**: An airport identified by a three-character code (e.g., "YYZ" for Toronto, "JFK" for New York, "LAX" for Los Angeles).

- **Flight Number**: The flight identifier consisting of 1-4 digits (e.g., "100", "1234").

- **Flight Suffix**: An optional letter after the flight number (e.g., "A" in flight AC100A).

- **Flight Leg**: A single scheduled segment from one airport to another. Legs sharing an aircraft can have different flight numbers, dates, and codeshares.

- **Leg Type**:
    - **Originating** = First leg of the flight
    - **Through** = Connecting leg in a multi-leg flight

- **Aircraft Type**: The model of aircraft (e.g., "73G" for Boeing 737-700, "321" for Airbus A321).

- **Registration**: The unique identifier assigned to an individual aircraft (e.g., "C-FCEU" or "N38955").

- **Codeshare**: An agreement where another airline markets the same operated flight under its own carrier code and flight number.

## Message Actions

- **CreateFlight** = Create a new flight schedule
- **ReplaceFlight** = Replace an existing flight schedule
- **UpdateTiming** = Update only timing information
- **CancelFlight** = Cancel a flight
- **ReinstateFlight** = Reinstate a cancelled flight
- **UpdateEquipment** = Update aircraft information
- **GateOutage** = Mark a gate unavailable for a time window (a resource message, not a flight message)
- **DivertFlight** = An unscheduled inbound needing a gate or remote stand soon; turnbacks have gate priority

---

## Creating a New Flight

### Example - Simple Direct Flight

```json
{
  "action": "CreateFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-GEPB",
    "aircraftType": "73G"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "123",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YYZ",
      "arrivalStation": "YVR",
      "scheduledDepartureTime": "12:00",
      "scheduledArrivalTime": "14:30"
    }
  ]
}
```

**Plain English**:
> "Create a new flight schedule for Air Canada flight 123 on June 15th, 2025. It's a single-leg flight using a Boeing 737-700 registered C-GEPB, departing Toronto at 12:00 PM and arriving in Vancouver at 2:30 PM local time."

---

### Example - Multi-Leg Flight Route

```json
{
  "action": "CreateFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-FCEU",
    "aircraftType": "321"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "456",
      "flightSuffix": null,
      "originatingDate": "2025-08-20",
      "departureStation": "YYZ",
      "arrivalStation": "YUL",
      "scheduledDepartureTime": "08:00",
      "scheduledArrivalTime": "10:00"
    },
    {
      "legType": "Through",
      "flightNumber": "457",
      "flightSuffix": null,
      "originatingDate": "2025-08-20",
      "departureStation": "YUL",
      "arrivalStation": "YYZ",
      "scheduledDepartureTime": "11:00",
      "scheduledArrivalTime": "13:00"
    }
  ]
}
```

**Plain English**:
> "Create a two-leg schedule for Air Canada's Airbus A321 registered C-FCEU on August 20th, 2025:
> - First leg, AC456: Toronto to Montreal, departing 8:00 AM arriving 10:00 AM
> - Second leg, AC457: Montreal to Toronto, departing 11:00 AM arriving 1:00 PM"

---

### Example - Flight with Codeshare Agreements

```json
{
  "action": "CreateFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-FNOE",
    "aircraftType": "789"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "800",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "codeshare": [
        {
          "carrier": "UA",
          "flightNumber": "8322"
        },
        {
          "carrier": "LH",
          "flightNumber": "6587"
        }
      ],
      "departureStation": "YYZ",
      "arrivalStation": "LHR",
      "scheduledDepartureTime": "18:30",
      "scheduledArrivalTime": "06:25"
    }
  ]
}
```

**Plain English**:
> "Create Air Canada flight 800 on June 15th, 2025, using a Boeing 787-9 registered C-FNOE. It departs Toronto at 6:30 PM and arrives in London Heathrow at 6:25 AM local time the next day. United markets the same flight as UA8322, and Lufthansa markets it as LH6587. These are three flight numbers for one operated flight."

The codeshare flight numbers in this example are illustrative.

---

## Field Descriptions

### Action Field

**`action`** (string, required)

Specifies the type of operation to perform:
- `"CreateFlight"` = Create a new flight schedule (equivalent to ASM NEW)
- `"ReplaceFlight"` = Replace existing schedule (equivalent to ASM RPL)
- `"UpdateTiming"` = Update timing only (equivalent to ASM TIM)
- `"CancelFlight"` = Cancel a flight (equivalent to ASM CNL)
- `"ReinstateFlight"` = Reinstate cancelled flight (equivalent to ASM RIN)
- `"UpdateEquipment"` = Update assigned aircraft and aircraft type (equivalent to ASM EQT)

---

### Emergency State Field

**`emergencyState`** (string, optional, top level)

May accompany any flight message, alongside `action`, `flight`, and `legs`. It is
not valid on the resource message `GateOutage`. The case-sensitive values are:

- `"Turnback"` — an aircraft returning to YYZ; prioritize a compatible terminal gate.
- `"Emergency"` — another emergency; a compatible remote stand is a valid assignment.

Omit this field when no emergency state is supplied. `null`, other types, and unknown
values are invalid. `reason` remains separate explanatory text. When both fields are
present, `emergencyState` controls allocation policy even if `reason` contains a
different classification. The stream preserves the field without assigning a resource.

For older `DivertFlight` messages without `emergencyState`, retain the reason-based
policy described under [gate priority and remote stands](#gate-priority-and-remote-stands).
Other flight messages without the field keep their existing behavior.

---

### Flight Object

The `flight` object contains only the aircraft details shared by every leg in the message. Flight numbers, dates, and codeshares belong to individual legs.

**`carrier`** (string, required)
- Two-character airline code (e.g., "AC", "UA", "BA")
- The airline operating all legs in this message

**`registration`** (string, required)
- Unique aircraft registration (tail number), identifying the aircraft rather than the flight
- Country prefix followed by letters and/or digits, with a hyphen where applicable (e.g., "C-FCEU", "9M-MRO", "N38955")

**`aircraftType`** (string, required)
- 3-character IATA aircraft type code (e.g., "73G", "321", "789")
- Static size-class and service-role references are in `aircraftTypes.records` in [data/aircraft.json](data/aircraft.json), keyed by `aircraftType`; shared codes can require additional model or service information
- The model of the registered aircraft, shared by all legs in this message

For `UpdateEquipment`, `flight` contains the new aircraft registration and type. The leg reference identifies the existing schedule to update. Changing aircraft for only one leg separates the schedule into connected groups of legs sharing an aircraft; a single `flight` object must never describe different aircraft.

---

### Legs Array

The `legs` array contains one or more flight leg objects. Each leg represents a segment from one airport to another and carries its own operating flight number, date, and optional codeshares. The operating carrier comes from `flight.carrier`.

`CreateFlight`, `ReplaceFlight`, `UpdateTiming`, and `DivertFlight` require complete leg objects. `CancelFlight`, `ReinstateFlight`, and `UpdateEquipment` use exactly one **leg reference** containing `flightNumber`, optional `flightSuffix`, `originatingDate`, `departureStation`, and `arrivalStation`; it identifies an existing leg without resending its times or `legType`.

A route is identified by `flight.carrier` and its first leg's `flightNumber`, `flightSuffix`, and `originatingDate`. These identifiers remain unchanged when replacing a route; its stations and scheduled times can change. An omitted suffix is equivalent to `null`.

For other modifications, the operating carrier plus the referenced leg's flight number, suffix, local departure date, and station pair identify the existing leg. For an `AllLegs` operation or reinstatement, the reference locates the stored route containing that leg; for `SpecificLeg`, only the referenced leg is affected. Aircraft registration is not part of this lookup, so equipment changes can assign a new aircraft.

**`legType`** (string, required for complete leg objects)
- `"Originating"` = First leg of the flight
- `"Through"` = Subsequent connecting legs
- First leg must always be "Originating"

**`flightNumber`** (string, required)
- 1-4 digit flight identifier for this leg (e.g., "100", "1234")
- Combined with `flight.carrier`, gives the operating flight number (e.g., AC456)
- Can differ between legs using the same aircraft

**`flightSuffix`** (string, optional)
- Single letter suffix (e.g., "A", "B") for this leg's flight number
- Use `null` or omit if no suffix
- Used when multiple versions of the same flight number exist

**`originatingDate`** (string, required)
- ISO 8601 date format: "YYYY-MM-DD" (e.g., "2025-06-15")
- The local departure date of this leg, which can differ from other legs' dates

**`codeshare`** (array of objects, optional)
- Marketing airlines and their flight numbers for this leg only
- Each object requires `carrier` (two-character airline code) and `flightNumber` (string of 1-4 digits)
- The parent `flight.carrier` and this leg's `flightNumber` identify the operating flight
- Omit this field or use an empty array `[]` when this leg has no codeshare agreements

**`departureStation`** (string, required)
- Three-character airport code (e.g., "YYZ", "JFK", "LAX")
- Where this leg departs from

**`arrivalStation`** (string, required)
- Three-character airport code (e.g., "YVR", "LHR", "AMS")
- Where this leg arrives

**`scheduledDepartureTime`** (string, required for complete leg objects)
- 24-hour time format: "HH:MM" (e.g., "08:00", "14:30", "23:45")
- Local time at the departure station

**`scheduledArrivalTime`** (string, required for complete leg objects)
- 24-hour time format: "HH:MM" (e.g., "10:00", "17:15", "01:30")
- Local time at the arrival station
- Can be next day if overnight (indicated by time earlier than departure)

---

## Complete Examples

### Example 1: Create Three-Leg Flight Route

```json
{
  "action": "CreateFlight",
  "flight": {
    "carrier": "UA",
    "registration": "N38955",
    "aircraftType": "789"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "1000",
      "flightSuffix": null,
      "originatingDate": "2025-09-01",
      "departureStation": "JFK",
      "arrivalStation": "LAX",
      "scheduledDepartureTime": "09:00",
      "scheduledArrivalTime": "12:00"
    },
    {
      "legType": "Through",
      "flightNumber": "1001",
      "flightSuffix": null,
      "originatingDate": "2025-09-01",
      "departureStation": "LAX",
      "arrivalStation": "SFO",
      "scheduledDepartureTime": "13:30",
      "scheduledArrivalTime": "15:00"
    },
    {
      "legType": "Through",
      "flightNumber": "1002",
      "flightSuffix": null,
      "originatingDate": "2025-09-01",
      "departureStation": "SFO",
      "arrivalStation": "JFK",
      "scheduledDepartureTime": "16:30",
      "scheduledArrivalTime": "00:45"
    }
  ]
}
```

**Plain English**:
> "Create a three-leg schedule for United's Boeing 787-9 registered N38955 on September 1st, 2025:
> - First leg, UA1000: JFK to LAX, departing 9:00 AM arriving 12:00 PM
> - Second leg, UA1001: LAX to SFO, departing 1:30 PM arriving 3:00 PM
> - Third leg, UA1002: SFO to JFK, departing 4:30 PM arriving 12:45 AM (next day)"

---

### Example 2: Replace Flight Schedule

```json
{
  "action": "ReplaceFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-GEOQ",
    "aircraftType": "73G"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "100",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YYZ",
      "arrivalStation": "YUL",
      "scheduledDepartureTime": "16:00",
      "scheduledArrivalTime": "18:00"
    }
  ]
}
```

**Plain English**:
> "Replace the existing schedule for Air Canada flight 100, using aircraft C-GEOQ, on June 15th, 2025 with a new departure time of 4:00 PM and arrival time of 6:00 PM."

---

### Example 3: Update Flight Timing

```json
{
  "action": "UpdateTiming",
  "flight": {
    "carrier": "AC",
    "registration": "C-FCEI",
    "aircraftType": "321"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "200",
      "flightSuffix": null,
      "originatingDate": "2025-08-20",
      "departureStation": "YYZ",
      "arrivalStation": "YUL",
      "scheduledDepartureTime": "08:15",
      "scheduledArrivalTime": "10:15"
    }
  ]
}
```

**Plain English**:
> "Update only the timing for Air Canada flight 200, using aircraft C-FCEI, on August 20th, 2025. New departure is 8:15 AM, new arrival is 10:15 AM."

---

## Cancel Flight Messages

### Example 1: Cancel Entire Flight

```json
{
  "action": "CancelFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-GEOQ",
    "aircraftType": "73G"
  },
  "legs": [
    {
      "flightNumber": "100",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YYZ",
      "arrivalStation": "YUL"
    }
  ],
  "cancelScope": {
    "type": "AllLegs"
  }
}
```

**Plain English**:
> "Cancel all legs of Air Canada flight 100, using aircraft C-GEOQ, on June 15th, 2025."

---

### Example 2: Cancel Specific Leg

```json
{
  "action": "CancelFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-FCEI",
    "aircraftType": "321"
  },
  "legs": [
    {
      "flightNumber": "200",
      "flightSuffix": null,
      "originatingDate": "2025-08-20",
      "departureStation": "YUL",
      "arrivalStation": "YYZ"
    }
  ],
  "cancelScope": {
    "type": "SpecificLeg"
  }
}
```

**Plain English**:
> "Cancel the specific leg of Air Canada flight 200, using aircraft C-FCEI, on August 20th, 2025 that goes from Montreal (YUL) to Toronto (YYZ)."

---

## Reinstate Flight Message

```json
{
  "action": "ReinstateFlight",
  "flight": {
    "carrier": "AC",
    "registration": "C-GEOQ",
    "aircraftType": "73G"
  },
  "legs": [
    {
      "flightNumber": "100",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YYZ",
      "arrivalStation": "YUL"
    }
  ]
}
```

**Plain English**:
> "Reinstate Air Canada flight 100, using aircraft C-GEOQ, on June 15th, 2025 that was previously cancelled. All legs return to scheduled status."

---

## Update Equipment Messages

### Example 1: Update Equipment for All Legs

```json
{
  "action": "UpdateEquipment",
  "flight": {
    "carrier": "AC",
    "registration": "C-FNOE",
    "aircraftType": "789"
  },
  "legs": [
    {
      "flightNumber": "100",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YYZ",
      "arrivalStation": "YUL"
    }
  ],
  "equipmentUpdate": {
    "scope": "AllLegs"
  }
}
```

**Plain English**:
> "Update the aircraft for all legs of Air Canada flight 100 on June 15th, 2025 to a Boeing 787-9 registered C-FNOE."

---

### Example 2: Update Equipment for Specific Leg

```json
{
  "action": "UpdateEquipment",
  "flight": {
    "carrier": "AC",
    "registration": "C-GEPJ",
    "aircraftType": "73G"
  },
  "legs": [
    {
      "flightNumber": "200",
      "flightSuffix": null,
      "originatingDate": "2025-08-20",
      "departureStation": "YYZ",
      "arrivalStation": "YUL"
    }
  ],
  "equipmentUpdate": {
    "scope": "SpecificLeg"
  }
}
```

**Plain English**:
> "Update the aircraft for the Toronto to Montreal leg of Air Canada flight 200 on August 20th, 2025 to a Boeing 737-700 registered C-GEPJ."

---

## Gate Outage Messages

A **GateOutage** is a *resource* message, not a flight message. It announces that a
specific gate will be unavailable for a time window (a maintenance event, equipment
failure, or operational closure). It has **no `flight` object** and **no `legs`** — it
carries the gate id and the outage window instead.

```json
{
  "action": "GateOutage",
  "gate": "D1",
  "scheduledStartTime": "10:00",
  "scheduledEndTime": "15:00"
}
```

**Plain English**:
> "Gate D1 is out of service from 10:00 AM to 3:00 PM. No flight may occupy it during
> that window, and any flight already assigned there must be moved."

- **`gate`** (string, required) — the gate id, e.g. `"D1"`.
- **`scheduledStartTime`** (string, required) — `HH:MM`, when the gate goes offline.
- **`scheduledEndTime`** (string, optional) — `HH:MM`, when it returns. If omitted, the
  outage lasts until further notice. May be earlier than the start time if the window
  crosses midnight.

Like flight messages, a GateOutage appears at an info-delivery time in the schedule, so
the system only learns about it when it is announced.

---

## Divert Flight Messages

A **DivertFlight** is an *unscheduled* inbound that was not in the filed schedule and now
needs a parking position soon — a medical emergency diversion, or an aircraft that departed
and has to turn back. It looks like a one-leg `CreateFlight` plus optional `emergencyState`
and `reason` fields. It usually arrives only a short time after it is announced. Turnbacks receive priority
for a terminal gate; other emergencies and diversions can use remote stands.

```json
{
  "action": "DivertFlight",
  "emergencyState": "Emergency",
  "reason": "medical",
  "flight": {
    "carrier": "QK",
    "registration": "C-FGJI",
    "aircraftType": "321"
  },
  "legs": [
    {
      "legType": "Originating",
      "flightNumber": "501",
      "flightSuffix": null,
      "originatingDate": "2025-06-15",
      "departureStation": "YQB",
      "arrivalStation": "YYZ",
      "scheduledDepartureTime": "10:00",
      "scheduledArrivalTime": "11:15"
    }
  ]
}
```

**Plain English**:
> "An A321 registered C-FGJI from Quebec City is diverting to Toronto for a medical emergency and will land
> at 11:15 — find it a compatible parking position, which may be a remote stand."

- **`emergencyState`** (string, optional) — `"Turnback"` or `"Emergency"`, as described above.
- **`reason`** (string, optional) — explanatory text, such as `"medical"`, `"technical"`, or
  `"weather"`. New messages use `emergencyState` to control gate priority.
- **`legs`** — one inbound leg ending at YYZ, same shape as a flight leg.
- A turnback (a plane that left YYZ and returns) is modeled the same way: the returning
  leg has both `departureStation` and `arrivalStation` set to `"YYZ"`, with the actual
  departure time and expected return time. A suffix like `"R"` can identify the return
  leg, but the suffix alone does not determine priority.

### Gate priority and remote stands

The matcher derives gate priority from `emergencyState`. No gate or stand assignment
is sent in the message. These rules apply whenever a flight message supplies the field.

| `emergencyState` | Gate priority | Remote stand use |
| --- | --- | --- |
| `"Turnback"` | Priority over ordinary flights competing for a compatible terminal gate | Fallback if no compatible gate is feasible |
| `"Emergency"` | No special claim on a terminal gate | A compatible remote stand is a valid assignment |

For a `DivertFlight` that omits `emergencyState`, preserve the legacy policy:
`reason: "turnback"` gets gate priority; `"medical"` or `"diversion"` allows remote stands;
an omitted or unrecognized reason retains diversion gate priority with remote fallback.
An explicit `emergencyState` takes precedence over all of these legacy reason values.

Without an explicit emergency state, missing or unrecognized reasons do not opt an
emergency into the remote-stand policy. All diversions still need timely parking, and leaving one unassigned costs a
heavier penalty than an ordinary flight. A valid remote-stand assignment counts as assigned;
a turnback still prefers a feasible terminal gate over a remote stand.

Gate priority never overrides aircraft compatibility, occupancy, or outages. Remote stands
are listed in `ifatcStands.records` in [data/pearson.json](data/pearson.json), with names such
as `"Remote Stand H01"`; their use must satisfy the matcher's availability and compatibility
checks. The stream announces the emergency state, reason, and times; the matcher chooses
the gate or stand.

---

## JSON Schema Notes

### Required vs Optional Fields

**Always Required:**
- `action`
- `flight` object containing `carrier`, `registration`, and `aircraftType`, for every flight message
- `legs` array for every flight message, using complete leg objects or a leg reference as described above
- `GateOutage` is a resource message and has neither `flight` nor `legs`

**Conditionally Required:**
- Complete leg objects with `legType` and scheduled times - Required for `CreateFlight`, `ReplaceFlight`, `UpdateTiming`, and `DivertFlight`
- Exactly one leg reference - Required for `CancelFlight`, `ReinstateFlight`, and `UpdateEquipment`
- `cancelScope` - Required for `CancelFlight` action
- `equipmentUpdate` - Required for `UpdateEquipment` action

**Optional:**
- `legs[].flightSuffix` - Can be `null` or omitted if not applicable
- `legs[].codeshare` - Array of marketing carrier/flight-number pairs for that leg; omit or use `[]` when there are no codeshare agreements
- `emergencyState` - Top-level `"Turnback"` or `"Emergency"` on flight messages; omit when not supplied
- `reason` - Explanatory text for `DivertFlight`; older messages without `emergencyState` retain the reason-based allocation policy

---

### Data Types

- **Strings**: Used for codes, identifiers, times
- **Arrays**: Used for legs and codeshare collections
- **Objects**: Used for complex structures (flight, legs, codeshare entries, cancelScope, equipmentUpdate)
- **ISO 8601 Dates**: Used for originating date (YYYY-MM-DD format)
- **24-hour Time**: Used for departure/arrival times (HH:MM format)

---

### Naming Convention

- Property names use camelCase (e.g., `departureStation`, `aircraftType`)
- Enum values use PascalCase (e.g., `"Originating"`, `"Through"`, `"AllLegs"`)
- All airline/airport codes in UPPERCASE

---

### Validation Rules

1. **Carrier**: Must be exactly 2 characters
2. **Flight Number**: Each leg's `flightNumber` must be a string of 1-4 digits
3. **Flight Suffix**: Each leg's `flightSuffix` must be exactly 1 letter, `null`, or omitted
4. **Station Codes**: Must be exactly 3 characters
5. **Aircraft Type**: `flight.aircraftType` must be exactly 3 characters and apply to every leg represented by that aircraft
6. **Times**: Must be in HH:MM format (00:00 to 23:59)
7. **Dates**: Each leg's `originatingDate` must be a valid ISO 8601 local departure date (YYYY-MM-DD)
8. **Leg Type**: Must be "Originating" or "Through"
9. **First Leg**: The first complete leg object must have legType "Originating"; leg references omit legType
10. **Subsequent Legs**: Subsequent complete leg objects must have legType "Through"
11. **Leg Connections**: In a complete route, arrival station of one leg must match departure station of next leg
12. **Cancel Scope Type**: Must be "AllLegs" or "SpecificLeg"
13. **Equipment Scope**: Must be "AllLegs" or "SpecificLeg"
14. **Registration**: Must be a non-empty uppercase string in the aircraft's national registration format (e.g., "C-FCEU" or "N38955")
15. **Codeshare**: Each leg's optional `codeshare` must be an array of objects, each with a required two-character uppercase `carrier` code and a required `flightNumber` string of 1-4 digits; it applies only to that leg

---

## Message Processing Rules

When working with JSON flight messages:

1. **CreateFlight** requires complete leg information for all legs
2. **ReplaceFlight** replaces the entire schedule for the stored route identified by the first leg - provide all new leg information
3. **UpdateTiming** updates only times - aircraft and stations from existing schedule
4. **CancelFlight** with "AllLegs" cancels entire flight
5. **CancelFlight** with "SpecificLeg" cancels only the leg identified in `legs`; its station pair belongs in that leg reference
6. **ReinstateFlight** only works on previously cancelled flights
7. **UpdateEquipment** with "AllLegs" assigns `flight.registration` and `flight.aircraftType` to every leg in the stored route identified by the leg reference; `equipmentUpdate` contains only the scope
8. **UpdateEquipment** with "SpecificLeg" assigns the new aircraft only to the referenced leg; remaining legs retain their aircraft, and the resulting schedule is represented as separate connected groups of legs sharing an aircraft
9. **Leg dates** are local departure dates at each leg's departure station, independently of other legs
10. **Times** are in 24-hour format and in local time at each respective station
11. **Multi-leg flights** must have connecting legs (arrival of one = departure of next)
12. **Codeshare flights** use the associated leg's schedule and shared aircraft; marketing flight numbers are aliases for that leg and do not create additional flights or gate assignments
