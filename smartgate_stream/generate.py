"""Seeded, synthetic Pearson traffic; no gate allocation or matcher state."""

import copy
import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .validation import DEFAULT_RATE, positive_number, require, validate_scenario


DATA = Path(__file__).resolve().parents[1] / "data"
TORONTO = ZoneInfo("America/Toronto")
PARTNERS = {"AC": "UA", "UA": "AC", "WS": "DL", "DL": "WS", "PD": "TS",
            "TS": "PD", "AA": "BA", "BA": "AA", "LH": "AC", "AF": "DL", "EK": "WS"}


def generate(seed, start_date, flight_count=1200, rate=DEFAULT_RATE):
    require(type(seed) is int, "seed must be an integer")
    rate = positive_number(rate, "rate")
    require(isinstance(start_date, str), "date must be YYYY-MM-DD")
    day = date.fromisoformat(start_date)
    require(day.isoformat() == start_date, "date must be YYYY-MM-DD")
    require(type(flight_count) is int and 1 <= flight_count <= 9999,
            "flight count must be between 1 and 9999")
    rng = random.Random(seed)
    profiles = json.loads((DATA / "stream_profiles.json").read_text(encoding="utf-8"))
    airports = profiles["airports"]
    zones = {station: ZoneInfo(info["timeZone"]) for station, info in airports.items()}
    carriers = profiles["carriers"]
    fleet_snapshot = json.loads((DATA / "opensky_fleet_2025.json").read_text(encoding="utf-8"))
    fleet = defaultdict(list)
    for aircraft in fleet_snapshot["records"]:
        fleet[(aircraft["carrier"], aircraft["aircraftType"])].append(aircraft["registration"])
    start = datetime.combine(day, time(), TORONTO).astimezone(timezone.utc)
    next_day = datetime.combine(day + timedelta(days=1), time(), TORONTO).astimezone(timezone.utc)
    events, inbound_flights, outbound_flights = [], [], []
    reservations = defaultdict(list)
    numbers = {p["carrier"]: p["flightNumberStart"] for p in carriers}
    codeshare_numbers = {p["carrier"]: 7000 for p in carriers}

    def add(when, message):
        events.append({"timestamp": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "message": copy.deepcopy(message)})

    def allocate(carrier, service, begin, end, station):
        eligible = {}
        for aircraft in service["aircraft"]:
            tails = [tail for tail in fleet[(carrier, aircraft)] if all(
                previous_station == station and (end + timedelta(minutes=45) <= previous_begin
                                                  or begin >= previous_end + timedelta(minutes=45))
                for previous_begin, previous_end, previous_station in reservations[tail])]
            if tails:
                eligible[aircraft] = tails
        if not eligible:
            return None
        aircraft = rng.choices(list(eligible), weights=[service["aircraft"][t] for t in eligible])[0]
        tail = rng.choice(eligible[aircraft])
        reservations[tail].append((begin, end, station))
        return aircraft, tail

    def message_for(profile, aircraft, tail, origin, destination, departure, arrival):
        carrier = profile["carrier"]
        number = numbers[carrier]
        require(number <= 9999, "scenario exceeds a carrier's flight-number capacity")
        numbers[carrier] += 1
        local_departure = departure.astimezone(zones[origin])
        leg = {"legType": "Originating", "flightNumber": str(number), "flightSuffix": None,
               "originatingDate": local_departure.date().isoformat(),
               "departureStation": origin, "arrivalStation": destination,
               "scheduledDepartureTime": local_departure.strftime("%H:%M"),
               "scheduledArrivalTime": arrival.astimezone(zones[destination]).strftime("%H:%M")}
        if carrier in PARTNERS and rng.random() < 0.35:
            partner = PARTNERS[carrier]
            alias = codeshare_numbers[partner]
            require(alias <= 9999, "scenario exceeds a carrier's codeshare-number capacity")
            codeshare_numbers[partner] += 1
            leg["codeshare"] = [{"carrier": partner, "flightNumber": str(alias)}]
        return {"action": "CreateFlight",
                "flight": {"carrier": carrier, "registration": tail, "aircraftType": aircraft}, "legs": [leg]}

    # Keep real aircraft available for the unscheduled inbound cases too.
    for _ in range(flight_count // 600):
        profile = carriers[0]
        service = profile["services"][0]
        arrival = datetime.combine(day, time(rng.randint(8, 20), rng.randrange(60)), TORONTO).astimezone(timezone.utc)
        reason = rng.choice(["turnback", "medical", "diversion"])
        emergency_state = "Turnback" if reason == "turnback" else "Emergency"
        origin = "YYZ" if emergency_state == "Turnback" else "YUL"
        equipment = allocate(profile["carrier"], service, arrival - timedelta(minutes=80), next_day, origin)
        require(equipment is not None, "snapshot has no available aircraft for diversions")
        aircraft, tail = equipment
        divert = message_for(profile, aircraft, tail, origin, "YYZ",
                             arrival - timedelta(minutes=80), arrival)
        divert.update(action="DivertFlight", emergencyState=emergency_state, reason=reason)
        if emergency_state == "Turnback":
            divert["legs"][0]["flightSuffix"] = "R"
        add(arrival - timedelta(seconds=rng.randint(15 * 60, 30 * 60)), divert)

    rotations = (flight_count + 1) // 2
    chosen = list(carriers) if rotations >= len(carriers) else []
    counts = Counter(p["carrier"] for p in chosen)
    while len(chosen) < rotations:
        available = [p for p in carriers if counts[p["carrier"]] < p.get("maxRotations", rotations)]
        profile = rng.choices(available, weights=[p["weight"] for p in available])[0]
        chosen.append(profile)
        counts[profile["carrier"]] += 1
    rng.shuffle(chosen)

    for index, profile in enumerate(chosen):
        for attempt in range(1000):
            if attempt and attempt % 100 == 0:
                counts[profile["carrier"]] -= 1
                available = [p for p in carriers if counts[p["carrier"]] < p.get("maxRotations", rotations)]
                profile = rng.choices(available, weights=[p["weight"] for p in available])[0]
                counts[profile["carrier"]] += 1
            service = rng.choices(profile["services"], weights=[s["weight"] for s in profile["services"]])[0]
            station = rng.choice(service["stations"])
            hours = service.get("arrivalHours", list(range(6, 21)))
            arrival = datetime.combine(day, time(rng.choice(hours), rng.randrange(60)), TORONTO).astimezone(timezone.utc)
            duration = timedelta(minutes=airports[station]["minutes"] + rng.randint(-10, 10))
            turnaround = rng.randint(*service.get("turnaroundMinutes", [60, 100]))
            departure = arrival + timedelta(minutes=turnaround)
            # Reserve delay headroom so a later update cannot double-book a real tail.
            equipment = allocate(profile["carrier"], service, arrival - duration,
                                 departure + duration + timedelta(minutes=90), station)
            if equipment is not None:
                break
        else:
            raise ValueError(f"cannot fit {flight_count} flights into the snapshot fleet for {profile['carrier']}; reduce --flights")
        aircraft, tail = equipment
        inbound = message_for(profile, aircraft, tail, station, "YYZ", arrival - duration, arrival)
        announcement = start + timedelta(seconds=rng.randrange(180 * 60))
        add(announcement, inbound)
        inbound_flights.append((inbound, arrival))
        if index * 2 + 1 >= flight_count:
            continue
        outbound = message_for(profile, aircraft, tail, "YYZ", station, departure, departure + duration)
        add(announcement, outbound)
        outbound_flights.append((outbound, service, departure, departure + duration))

    for message, service, departure, arrival in outbound_flights:
        action = rng.choices([None, "UpdateTiming", "CancelFlight", "UpdateEquipment", "ReplaceFlight"],
                             weights=[50, 25, 8, 8, 9])[0]
        if action is None:
            continue
        when = departure - timedelta(seconds=rng.randint(30 * 60, 90 * 60))
        update = copy.deepcopy(message)
        update["action"] = action
        if action in ("UpdateTiming", "ReplaceFlight"):
            latest_delay = min(90, int((next_day - departure).total_seconds() / 60) - 1)
            delay = timedelta(minutes=rng.randint(1, latest_delay))
            leg = update["legs"][0]
            leg["scheduledDepartureTime"] = (departure + delay).astimezone(TORONTO).strftime("%H:%M")
            leg["scheduledArrivalTime"] = (arrival + delay).astimezone(zones[leg["arrivalStation"]]).strftime("%H:%M")
        else:
            update["legs"] = [{k: v for k, v in message["legs"][0].items()
                               if k in ("flightNumber", "flightSuffix", "originatingDate", "departureStation", "arrivalStation")}]
            if action == "UpdateEquipment":
                equipment = allocate(message["flight"]["carrier"], service,
                                     departure - timedelta(minutes=45), arrival + timedelta(minutes=90),
                                     message["legs"][0]["arrivalStation"])
                if equipment is None:
                    continue
                update["flight"]["aircraftType"], update["flight"]["registration"] = equipment
                update["equipmentUpdate"] = {"scope": rng.choice(["AllLegs", "SpecificLeg"])}
            else:
                update["cancelScope"] = {"type": rng.choice(["AllLegs", "SpecificLeg"])}
        add(when, update)
        if action == "CancelFlight" and rng.random() < 0.5:
            update["action"] = "ReinstateFlight"
            del update["cancelScope"]
            add(when + timedelta(seconds=rng.randint(5 * 60, 20 * 60)), update)

    pearson = json.loads((DATA / "pearson.json").read_text(encoding="utf-8"))
    gates = [group[0] for terminal in pearson["passengerGateGroups"]["terminals"].values()
             for group in terminal["groups"]]
    for _ in range(max(1, flight_count // 120)):
        outage_start = datetime.combine(day, time(rng.randint(6, 20), rng.randrange(60)), TORONTO)
        outage = {"action": "GateOutage", "gate": rng.choice(gates),
                  "scheduledStartTime": outage_start.strftime("%H:%M")}
        if rng.random() < 0.8:
            outage["scheduledEndTime"] = (outage_start + timedelta(minutes=rng.randint(30, 180))).strftime("%H:%M")
        add(outage_start - timedelta(seconds=rng.randint(0, 30 * 60)), outage)

    for message, arrival in inbound_flights:
        if rng.random() >= 0.25:
            continue
        early_arrival = arrival - timedelta(minutes=rng.randint(1, 30))
        update = copy.deepcopy(message)
        update["action"] = "UpdateTiming"
        update["legs"][0]["scheduledArrivalTime"] = early_arrival.astimezone(TORONTO).strftime("%H:%M")
        add(early_arrival - timedelta(seconds=rng.randint(5 * 60, 20 * 60)), update)

    events.sort(key=lambda event: event["timestamp"])
    scenario = {"metadata": {"seed": seed, "date": start_date, "flight_count": flight_count, "rate": rate,
                             "synthetic": True, "profiles": "data/stream_profiles.json",
                             "fleet_snapshot": fleet_snapshot["source"],
                             "fleet_source_sha256": fleet_snapshot["sourceSha256"]}, "events": events}
    validate_scenario(scenario)
    return scenario
