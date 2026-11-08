#!/usr/bin/env python3
"""SmartGate matcher webhook receiver for the stream branch.

Run from the repository root:
    python matcher_server.py --host 127.0.0.1 --port 9000

The updater POSTs one message at a time to /updates. This process owns the
current schedule and assignments. It uses a joint JuMP/Gurobi ILP for
InitializeAirport and a local greedy repair for later events.
"""
from __future__ import annotations

import subprocess
import argparse
import base64
import copy
import hashlib
import json
import re
import socket
import struct
import os
import sys
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
JULIA = os.environ.get("GMS_JULIA", "julia")
SOLVER_BACKEND = Path(os.environ.get("GMS_JL_BACKEND", HERE / "solver_backend.jl"))
_DATA_DIR = HERE / "data"
AIRCRAFT_DEFAULT = _DATA_DIR / "aircraft.json" if (_DATA_DIR / "aircraft.json").is_file() else HERE / "aircraft.json"
PEARSON_DEFAULT = _DATA_DIR / "pearson.json" if (_DATA_DIR / "pearson.json").is_file() else HERE / "pearson.json"
AIRCRAFT_JSON = Path(os.environ.get("GMS_AIRCRAFT_JSON", AIRCRAFT_DEFAULT))
PEARSON_JSON = Path(os.environ.get("GMS_PEARSON_JSON", PEARSON_DEFAULT))
STATION_TZ_DEFAULT = _DATA_DIR / "station_timezones.json" if (_DATA_DIR / "station_timezones.json").is_file() else HERE / "station_timezones.json"
STATION_TZ_JSON = Path(os.environ.get("GMS_STATION_TIMEZONES_JSON", STATION_TZ_DEFAULT))
STREAM_PROFILES_JSON = _DATA_DIR / "stream_profiles.json"
AIRLINE_TERMINALS: dict[str, str] = {}
AIRLINE_NAMES: dict[str, str] = {}
JULIA_TIMEOUT = float(os.environ.get("GMS_JL_TIMEOUT", "9.0"))
WIDTH_CLASSES = ("A", "B", "C", "D", "E", "F")

# Built-in fallback mappings. The global airportsdata database supplies broad
# coverage for other IATA stations; this fallback keeps common routes working
# even before that optional package is installed. External JSON entries override
# these values, and YYZ is always taken from pearson.json.
BUILTIN_STATION_TIMEZONES = {
    'ATL': 'America/New_York',
    'BOS': 'America/New_York',
    'CDG': 'Europe/Paris',
    'CUN': 'America/Cancun',
    'DEN': 'America/Denver',
    'DFW': 'America/Chicago',
    'DTW': 'America/Detroit',
    'DXB': 'Asia/Dubai',
    'EWR': 'America/New_York',
    'FRA': 'Europe/Berlin',
    'IAD': 'America/New_York',
    'JFK': 'America/New_York',
    'LAX': 'America/Los_Angeles',
    'LGW': 'Europe/London',
    'LHR': 'Europe/London',
    'MCO': 'America/New_York',
    'MIA': 'America/New_York',
    'MSP': 'America/Chicago',
    'ORD': 'America/Chicago',
    'PHL': 'America/New_York',
    'SFO': 'America/Los_Angeles',
    'YEG': 'America/Edmonton',
    'YHZ': 'America/Halifax',
    'YOW': 'America/Toronto',
    'YQB': 'America/Toronto',
    'YQT': 'America/Toronto',
    'YUL': 'America/Toronto',
    'YVR': 'America/Vancouver',
    'YWG': 'America/Winnipeg',
    'YYC': 'America/Edmonton',
    'YYZ': 'America/Toronto',
}


class TimezoneCoverageError(Exception):
    """A YYZ-relevant route uses a station whose local timezone is unknown."""


def load_global_iata_timezones() -> dict[str, ZoneInfo]:
    """Load IATA airport timezones from the airportsdata package, if installed.

    The checked-in station_timezones.json remains the explicit project override.
    airportsdata supplies broad IATA coverage so new schedule files do not
    require hand-editing one station at a time.
    """
    try:
        import airportsdata  # type: ignore
    except ImportError:
        return {}

    try:
        records = airportsdata.load("IATA")
    except Exception as exc:
        raise RuntimeError(
            "Could not load the airportsdata IATA timezone database. "
            "Install/repair it with: python -m pip install --upgrade airportsdata"
        ) from exc

    result: dict[str, ZoneInfo] = {}
    for code, record in records.items():
        code_key = norm(code)
        zone_name = str(record.get("tz") or "").strip()
        if not code_key or not zone_name:
            continue
        try:
            result[code_key] = ZoneInfo(zone_name)
        except Exception:
            # Ignore malformed rows; coverage validation below will identify
            # any station that actually matters to this YYZ schedule.
            continue
    return result

def norm(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def leg_logical_key(carrier: str, leg: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        norm(carrier),
        str(leg.get("flightNumber", "")),
        str(leg.get("flightSuffix") or ""),
        str(leg.get("originatingDate", "")),
    )


def route_key(carrier: str, legs: list[dict[str, Any]]) -> tuple[str, str, str, str]:
    if not legs:
        raise ValueError("A flight route must contain at least one leg.")
    return leg_logical_key(carrier, legs[0])


def leg_token(route_id: tuple[str, str, str, str], index: int) -> str:
    return "|".join((*route_id, f"LEG{index}"))


def parse_hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except Exception as exc:
        raise ValueError(f"Invalid local time {value!r}; expected HH:MM.") from exc


def local_event_time(timestamp: str, airport_tz: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(airport_tz).replace(tzinfo=None)


def minutes_from_epoch(value: datetime, epoch: datetime) -> float:
    return (value - epoch).total_seconds() / 60.0


def load_references() -> tuple[dict[str, str], list[dict[str, Any]], str]:
    """Load only the current passenger-gate crosswalk and eligible aircraft data."""
    profiles_path = _DATA_DIR / "stream_profiles.json"
    for path in (AIRCRAFT_JSON, PEARSON_JSON, profiles_path):
        if not path.is_file():
            raise FileNotFoundError(f"Required matcher reference not found: {path}")

    aircraft_data = json.loads(AIRCRAFT_JSON.read_text(encoding="utf-8"))
    pearson_data = json.loads(PEARSON_JSON.read_text(encoding="utf-8"))
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    configured_types = {
        str(code).strip().upper()
        for carrier in profiles.get("carriers", [])
        for service in carrier.get("services", [])
        for code in service.get("aircraft", [])
    }
    aircraft_classes: dict[str, str] = {}
    for record in aircraft_data.get("aircraftTypes", {}).get("records", []):
        code = norm(record.get("aircraftType"))
        width_class = norm(record.get("widthClass"))
        # Eligibility is deliberately the exact Passenger-only classification
        # in aircraft.json, intersected with stream_profiles.json.
        if (code in configured_types
                and record.get("serviceTypes") == ["Passenger"]
                and width_class in WIDTH_CLASSES):
            aircraft_classes[code] = width_class

    terminal_by_gate: dict[str, str] = {}
    for terminal, terminal_data in pearson_data.get("passengerGateGroups", {}).get("terminals", {}).items():
        for group in terminal_data.get("groups", []):
            if group:
                terminal_by_gate[str(group[0]).strip()] = str(terminal)

    airline_terminals = {
        norm(row.get("carrier")): str(row.get("terminal"))
        for row in pearson_data.get("airlineTerminals", {}).get("records", [])
        if row.get("carrier") and row.get("terminal")
    }

    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in pearson_data.get("passengerStandMapping", {}).get("records", []):
        gate_id = str(record.get("gate", "")).strip()
        width_class = norm(record.get("widthClass"))
        conflict_group = str(record.get("conflictGroup", "")).strip()
        stands = [str(value).strip() for value in record.get("ifatcStandNames", []) if str(value).strip()]
        if not gate_id or not conflict_group or gate_id in seen or not stands:
            continue
        seen.add(gate_id)
        gates.append({
            "id": gate_id,
            "widthClass": width_class if width_class in WIDTH_CLASSES else None,
            "conflictGroup": conflict_group,
            "ifatcStandNames": stands,
            "terminal": terminal_by_gate.get(gate_id),
            "kind": "remote" if gate_id.startswith("Remote Stand H") else "gate",
            "basis": record.get("basis"),
        })

    gates.sort(key=lambda item: item["id"])
    if not gates:
        raise RuntimeError("No passenger gate resources were found in passengerStandMapping.records.")
    airport_timezone = pearson_data.get("airport", {}).get("timeZone")
    if not airport_timezone:
        raise ValueError("pearson.json is missing airport.timeZone.")

    # Keep the existing global airline-terminal reference available to the
    # scenario builder without treating it as an exclusive gate restriction.
    global AIRLINE_TERMINALS, AIRLINE_NAMES
    AIRLINE_TERMINALS = airline_terminals
    AIRLINE_NAMES = {
        norm(row.get("carrier")): str(row.get("name") or row.get("carrier") or "")
        for row in pearson_data.get("airlineTerminals", {}).get("records", [])
    }
    return aircraft_classes, gates, str(airport_timezone)


class ScenarioState:
    def __init__(self, scenario: dict[str, Any], aircraft_classes: dict[str, str],
                 gates: list[dict[str, str]], airport_timezone: str):
        self.scenario = scenario
        self.aircraft_classes = aircraft_classes
        self.gates = gates
        self.airport_tz = ZoneInfo(airport_timezone)
        # Timezone data is self-contained for common routes. If the optional
        # station_timezones.json exists, its entries are treated as explicit
        # project overrides. The global airportsdata package adds broad IATA
        # coverage for future schedules with new origin/destination airports.
        timezone_data = {}
        if STATION_TZ_JSON.is_file():
            timezone_data = json.loads(STATION_TZ_JSON.read_text(encoding="utf-8"))
        self.station_timezones = {
            norm(code): ZoneInfo(zone)
            for code, zone in BUILTIN_STATION_TIMEZONES.items()
        }
        global_iata_timezones = load_global_iata_timezones()
        self.station_timezones.update(global_iata_timezones)
        if STREAM_PROFILES_JSON.is_file():
            profiles = json.loads(STREAM_PROFILES_JSON.read_text(encoding="utf-8"))
            for code, airport in profiles.get("airports", {}).items():
                zone = str(airport.get("timeZone") or "").strip()
                if zone:
                    self.station_timezones[norm(code)] = ZoneInfo(zone)
        for code, zone in timezone_data.get("timeZones", {}).items():
            self.station_timezones[norm(code)] = ZoneInfo(zone)
        # YYZ always uses the airport timezone in pearson.json, not a second
        # independently maintained station entry.
        self.station_timezones["YYZ"] = self.airport_tz
        self.timezone_database = {
            "global_iata_database_loaded": bool(global_iata_timezones),
            "built_in_fallbacks": sorted(BUILTIN_STATION_TIMEZONES),
            "project_overrides": sorted(norm(code) for code in timezone_data.get("timeZones", {})),
        }
        self.gate_ids = {norm(g["id"]): g["id"] for g in gates}
        self.gate_by_id = {g["id"]: g for g in gates}
        self.stand_to_gate = {
            norm(stand): gate["id"]
            for gate in gates for stand in gate.get("ifatcStandNames", [])
        }
        self.stand_to_conflict_group = {
            norm(stand): gate["conflictGroup"]
            for gate in gates for stand in gate.get("ifatcStandNames", [])
        }
        self.airline_terminals = dict(AIRLINE_TERMINALS)
        self.token_legs: dict[str, dict[str, Any]] = {}
        self.parked_gates: dict[str, str] = {}
        self.routes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.parked: dict[str, dict[str, str]] = {}
        self.outages: list[dict[str, Any]] = []
        self.unmatched_outages: list[dict[str, Any]] = []
        self.recognized_passenger_gate_outages: list[dict[str, Any]] = []
        self.token_policy: dict[str, dict[str, Any]] = {}
        self.epoch: datetime | None = None
        self.horizon_end: datetime | None = None
        self.initialized = False

    def timezone_coverage_issues(self) -> list[dict[str, str]]:
        """Return every missing non-YYZ station timezone in active YYZ routes."""
        missing: dict[str, set[str]] = defaultdict(set)
        for route in self.routes.values():
            for entry in route["legs"]:
                if not entry["active"]:
                    continue
                leg = entry["leg"]
                dep_station = norm(leg.get("departureStation"))
                arr_station = norm(leg.get("arrivalStation"))
                if dep_station != "YYZ" and arr_station != "YYZ":
                    continue
                for station in (dep_station, arr_station):
                    if station and station != "YYZ" and station not in self.station_timezones:
                        missing[station].add(f"{dep_station}->{arr_station}")
                    elif not station:
                        missing["<missing station code>"].add(f"{dep_station}->{arr_station}")
        return [
            {"station": station, "routes": ", ".join(sorted(routes))}
            for station, routes in sorted(missing.items())
        ]

    def validate_timezone_coverage(self) -> None:
        issues = self.timezone_coverage_issues()
        if issues:
            details = "; ".join(
                f"{item['station']} (routes: {item['routes']})" for item in issues
            )
            raise TimezoneCoverageError(
                "Timezone coverage is incomplete for YYZ-relevant routes: " + details + ". "
                "Install the airportsdata package (python -m pip install airportsdata) "
                "or add the station's IATA-to-IANA timezone to station_timezones.json. "
                "Initialization was stopped before optimization; no assignments were changed."
            )

    def initialize(self, event: dict[str, Any]) -> None:
        message = event["message"]
        if message.get("action") != "InitializeAirport":
            raise ValueError("events[0].message must be InitializeAirport.")

        self.epoch = local_event_time(event["timestamp"], self.airport_tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.parked = {
            norm(item.get("registration")): {
                "carrier": norm(item.get("carrier")),
                "registration": norm(item.get("registration")),
                "aircraftType": norm(item.get("aircraftType")),
            }
            for item in message.get("parkedAircraft", [])
        }
        self.parked_gates = {
            norm(item.get("registration")): str(item.get("gate"))
            for item in message.get("parkedAircraft", []) if item.get("gate")
        }

        for flight_message in message.get("flights", []):
            action = flight_message.get("action")
            if action != "CreateFlight":
                raise ValueError(
                    f"InitializeAirport.flights must contain CreateFlight messages; got {action!r}."
                )
            self._create_route(flight_message)

        # Validate all YYZ-relevant route stations up front. This avoids a
        # late failure halfway through session construction or solver startup.
        self.validate_timezone_coverage()

        # Use a horizon that covers the full operating day and any overnight
        # YYZ arrival/departure present in the baseline schedule.
        schedule_times = self._all_yyz_event_times()
        latest = max(schedule_times, default=self.epoch)
        self.horizon_end = max(self.epoch + timedelta(hours=24), latest + timedelta(hours=3))

        for outage in message.get("gateOutages", []):
            self._add_outage(outage, self.epoch)

        self.initialized = True

    def _create_route(self, message: dict[str, Any]) -> None:
        flight = message.get("flight") or {}
        carrier = norm(flight.get("carrier"))
        legs = message.get("legs") or []
        key = route_key(carrier, legs)
        route_entries = []
        for index, leg in enumerate(legs):
            token = leg_token(key, index)
            policy = {
                "action": message.get("action", ""),
                "emergencyState": message.get("emergencyState"),
                "reason": message.get("reason"),
            }
            self.token_policy[token] = policy
            route_entries.append({
                "leg": dict(leg),
                "aircraft": dict(flight),
                "active": True,
                "cancelledAt": None,
                "token": token,
            })
            self.token_legs[token] = {
                "carrier": carrier,
                "leg": {
                    "flightNumber": leg.get("flightNumber"),
                    "flightSuffix": leg.get("flightSuffix"),
                    "originatingDate": leg.get("originatingDate"),
                    "departureStation": leg.get("departureStation"),
                    "arrivalStation": leg.get("arrivalStation"),
                    "trafficType": leg.get("trafficType"),
                },
            }
        self.routes[key] = {"carrier": carrier, "legs": route_entries}

    def _find_route_leg(self, carrier: str, reference: dict[str, Any]):
        target_logical = leg_logical_key(carrier, reference)
        target_dep = norm(reference.get("departureStation"))
        target_arr = norm(reference.get("arrivalStation"))
        fallback = None

        for route_id, route in self.routes.items():
            if route["carrier"] != norm(carrier):
                continue
            for index, entry in enumerate(route["legs"]):
                leg = entry["leg"]
                if leg_logical_key(carrier, leg) != target_logical:
                    continue
                if (
                    target_dep and target_arr
                    and norm(leg.get("departureStation")) == target_dep
                    and norm(leg.get("arrivalStation")) == target_arr
                ):
                    return route_id, index, entry
                fallback = (route_id, index, entry)
        return fallback

    def apply_message(self, event: dict[str, Any]) -> str:
        message = event["message"]
        action = message.get("action", "")
        if action == "GateOutage":
            event_local = local_event_time(event["timestamp"], self.airport_tz)
            self._add_outage(message, event_local)
            return action

        if action in ("CreateFlight", "DivertFlight"):
            self._create_route(message)
            return action

        flight = message.get("flight") or {}
        carrier = norm(flight.get("carrier"))
        legs = message.get("legs") or []
        if not legs:
            return action

        if action == "ReplaceFlight":
            new_route_key = route_key(carrier, legs)
            # The specification defines route identity by operating carrier
            # and first leg's flight number/suffix/originating date.
            self._create_route(message)
            return action

        found = self._find_route_leg(carrier, legs[0])
        if found is None:
            # Do not fabricate a new route for a modification whose reference
            # cannot be matched to the baseline schedule.
            return action

        route_id, index, entry = found

        if action == "UpdateTiming":
            updated = dict(legs[0])
            if "legType" not in updated:
                updated["legType"] = entry["leg"].get("legType", "Originating")
            entry["leg"] = updated
            self.token_legs[entry["token"]] = {
                "carrier": carrier,
                "leg": {
                    "flightNumber": updated.get("flightNumber"),
                    "flightSuffix": updated.get("flightSuffix"),
                    "originatingDate": updated.get("originatingDate"),
                    "departureStation": updated.get("departureStation"),
                    "arrivalStation": updated.get("arrivalStation"),
                    "trafficType": updated.get("trafficType"),
                },
            }
            # Aircraft details are present in the webhook and remain shared
            # for the updated leg.
            if flight:
                entry["aircraft"] = dict(flight)

        elif action == "CancelFlight":
            scope = norm((message.get("cancelScope") or {}).get("type"))
            if scope == "ALLLEGS":
                for item in self.routes[route_id]["legs"]:
                    item["active"] = False
                    item["cancelledAt"] = event["timestamp"]
            else:
                entry["active"] = False
                entry["cancelledAt"] = event["timestamp"]

        elif action == "ReinstateFlight":
            for item in self.routes[route_id]["legs"]:
                item["active"] = True
                item["cancelledAt"] = None

        elif action == "UpdateEquipment":
            scope = norm((message.get("equipmentUpdate") or {}).get("scope"))
            if scope == "ALLLEGS":
                for item in self.routes[route_id]["legs"]:
                    item["aircraft"] = dict(flight)
            else:
                entry["aircraft"] = dict(flight)

        return action

    def _add_outage(self, message: dict[str, Any], event_local: datetime) -> None:
        gate_raw = str(message.get("gate", "")).strip()
        gate_key = norm(gate_raw)
        gate_id = self.stand_to_gate.get(gate_key)
        conflict_group = self.stand_to_conflict_group.get(gate_key)
        if gate_id is None or conflict_group is None:
            raise ValueError(
                f"GateOutage {gate_raw!r} is not an exact IFATC stand in passengerStandMapping."
            )

        outage_date = event_local.date()
        start_dt = datetime.combine(outage_date, parse_hhmm(message["scheduledStartTime"]))
        end_raw = message.get("scheduledEndTime")
        if end_raw:
            end_dt = datetime.combine(outage_date, parse_hhmm(end_raw))
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
        else:
            end_dt = self.horizon_end or (start_dt + timedelta(days=1))

        record = {
            "gate": gate_id,
            "ifatc_stand": gate_raw,
            "conflictGroup": conflict_group,
            "start": minutes_from_epoch(start_dt, self.epoch),
            "end": minutes_from_epoch(end_dt, self.epoch),
            "lookup_status": "matched_exact_ifatc_stand",
            "resource_type": "passenger_conflict_group",
        }
        self.outages.append(record)
        self.recognized_passenger_gate_outages.append(record.copy())

    def _leg_datetimes(self, leg: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        # The contract gives local departure and arrival times at their
        # respective stations. Convert through UTC so date-line and DST
        # differences do not create false YYZ parking intervals.
        day = date.fromisoformat(leg["originatingDate"])
        dep_station = norm(leg.get("departureStation"))
        arr_station = norm(leg.get("arrivalStation"))
        if dep_station != "YYZ" and arr_station != "YYZ":
            return None, None
        dep_tz = self.airport_tz if dep_station == "YYZ" else self.station_timezones.get(dep_station)
        arr_tz = self.airport_tz if arr_station == "YYZ" else self.station_timezones.get(arr_station)
        if dep_tz is None or arr_tz is None:
            missing = dep_station if dep_tz is None else arr_station
            raise TimezoneCoverageError(
                f"No IANA timezone is available for station {missing!r} on "
                f"YYZ-relevant route {dep_station}->{arr_station}. "
                "Install airportsdata or add a verified mapping to station_timezones.json."
            )
        dep_local = datetime.combine(day, parse_hhmm(leg["scheduledDepartureTime"])).replace(tzinfo=dep_tz)
        dep_utc = dep_local.astimezone(timezone.utc)
        arr_clock = parse_hhmm(leg["scheduledArrivalTime"])
        arrival_candidates = [
            datetime.combine(day + timedelta(days=offset), arr_clock).replace(tzinfo=arr_tz)
            for offset in range(0, 3)
        ]
        valid_arrivals = [candidate for candidate in arrival_candidates
                          if candidate.astimezone(timezone.utc) > dep_utc]
        if not valid_arrivals:
            raise RuntimeError(f"Could not determine arrival date for {dep_station}->{arr_station}.")
        arr_local = min(valid_arrivals, key=lambda candidate: candidate.astimezone(timezone.utc))
        dep_yyz = (dep_local.astimezone(self.airport_tz).replace(tzinfo=None)
                   if dep_station == "YYZ" else None)
        arr_yyz = (arr_local.astimezone(self.airport_tz).replace(tzinfo=None)
                   if arr_station == "YYZ" else None)
        return dep_yyz, arr_yyz

    def _all_yyz_event_times(self) -> list[datetime]:
        values: list[datetime] = []
        for route in self.routes.values():
            for entry in route["legs"]:
                if not entry["active"]:
                    continue
                leg = entry["leg"]
                try:
                    dep, arr = self._leg_datetimes(leg)
                except (KeyError, ValueError):
                    continue
                if dep is not None:
                    values.append(dep)
                if arr is not None:
                    values.append(arr)
        return values

    def build_sessions(self) -> dict[str, dict[str, Any]]:
        if self.epoch is None or self.horizon_end is None:
            raise RuntimeError("Scenario state has not been initialized.")

        # Events are grouped by registration and represent the moments an
        # aircraft arrives at or departs from YYZ.
        by_registration: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for route in self.routes.values():
            for entry in route["legs"]:
                if not entry["active"]:
                    continue
                aircraft = entry["aircraft"]
                registration = norm(aircraft.get("registration"))
                if not registration:
                    continue
                leg = entry["leg"]
                try:
                    dep, arr = self._leg_datetimes(leg)
                except (KeyError, ValueError):
                    continue
                if arr is not None:
                    by_registration[registration].append({
                        "kind": "ARRIVE", "time": arr, "token": entry["token"],
                        "aircraft": aircraft,
                    })
                if dep is not None:
                    by_registration[registration].append({
                        "kind": "DEPART", "time": dep, "token": entry["token"],
                        "aircraft": aircraft,
                    })

        sessions: dict[str, dict[str, Any]] = {}
        horizon = self.horizon_end

        registrations = set(by_registration) | set(self.parked)
        for registration in registrations:
            events = sorted(
                by_registration.get(registration, []),
                key=lambda item: (item["time"], 0 if item["kind"] == "DEPART" else 1),
            )
            parked_aircraft = self.parked.get(registration)
            opened = None

            if parked_aircraft:
                # If an equipment update has assigned a different aircraft to
                # a scheduled departure, use that leg's aircraft metadata.
                first_departure = next(
                    (e for e in events if e["kind"] == "DEPART"), None
                )
                meta = first_departure["aircraft"] if first_departure else parked_aircraft
                opened = (self.epoch, f"PARKED:{registration}", meta)

            for item in events:
                if item["kind"] == "ARRIVE":
                    if opened is None:
                        opened = (item["time"], item["token"], item["aircraft"])
                else:  # DEPART
                    if opened is not None:
                        start_dt, anchor, meta = opened
                        if item["time"] > start_dt:
                            self._add_session(
                                sessions, anchor, registration, meta, start_dt, item["time"],
                                end_token=item["token"]
                            )
                        opened = None

            if opened is not None:
                start_dt, anchor, meta = opened
                if horizon > start_dt:
                    self._add_session(
                        sessions, anchor, registration, meta, start_dt, horizon,
                        end_token=None
                    )

        return sessions

    def _add_session(self, sessions, anchor, registration, aircraft, start, end, end_token=None):
        aircraft_type = norm(aircraft.get("aircraftType"))
        width_class = self.aircraft_classes.get(aircraft_type)
        session_id = anchor
        # If two active sessions ever resolve to the same anchor, append the
        # registration to keep the identifiers unique without changing normal
        # session identities during timing/equipment updates.
        if session_id in sessions:
            session_id = f"{anchor}|{registration}"
        policy = self.token_policy.get(anchor, {})
        is_diversion = policy.get("action") == "DivertFlight"
        emergency_state = policy.get("emergencyState")
        reason = str(policy.get("reason") or "").strip().lower()
        remote_allowed = bool(
            emergency_state == "Emergency"
            or (emergency_state is None and is_diversion and reason in ("medical", "diversion"))
        )
        turnback = bool(
            emergency_state == "Turnback"
            or (emergency_state is None and is_diversion and reason == "turnback")
        )
        priority = bool(is_diversion and not remote_allowed) or turnback
        sessions[session_id] = {
            "id": session_id,
            "registration": registration,
            "carrier": norm(aircraft.get("carrier")),
            "aircraftType": aircraft_type,
            "widthClass": width_class,
            "start": minutes_from_epoch(start, self.epoch),
            "end": minutes_from_epoch(end, self.epoch),
            "priority": priority,
            "remoteAllowed": remote_allowed,
            "turnback": turnback,
            "initiallyParked": anchor.startswith("PARKED:"),
            "start_token": None if anchor.startswith("PARKED:") else anchor,
            "end_token": end_token,
            "preferredTerminal": self.airline_terminals.get(norm(aircraft.get("carrier"))),
            "trafficTypes": sorted({
                str(self.token_legs[token]["leg"].get("trafficType"))
                for token in (anchor, end_token)
                if token in self.token_legs and self.token_legs[token]["leg"].get("trafficType")
            }),
        }

    def outage_payload(self) -> dict[str, list[list[float]]]:
        # Any outage on an alternative IFATC stand closes its whole canonical
        # passenger conflict group, exactly as the stream's GateState does.
        result: dict[str, list[list[float]]] = defaultdict(list)
        for outage in self.outages:
            group = outage.get("conflictGroup")
            if group:
                result[group].append([outage["start"], outage["end"]])
        return dict(result)


def run_julia(backend: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not backend.is_file():
        raise FileNotFoundError(f"Julia backend not found: {backend}")
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    try:
        proc = subprocess.run(
            [JULIA, str(backend)],
            input=encoded,
            text=True,
            capture_output=True,
            cwd=str(HERE),
            timeout=JULIA_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Julia executable {JULIA!r} was not found. Install Julia and add it to PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{backend.name} exceeded the {JULIA_TIMEOUT:g}-second timeout."
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"{backend.name} failed:\n{proc.stderr.strip() or proc.stdout.strip()}"
        )

    decoder = json.JSONDecoder()
    for pos, char in enumerate(proc.stdout):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(proc.stdout[pos:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "assignments" in obj:
            return obj
    raise RuntimeError(f"{backend.name} did not return JSON. Output: {proc.stdout[:2000]}")


def assignment_map(pairs: list[list[str]]) -> dict[str, str]:
    return {str(pair[0]): str(pair[1]) for pair in pairs}


def intervals_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return float(a["start"]) < float(b["end"]) and float(b["start"]) < float(a["end"])


def determine_changed_sessions(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    outages: dict[str, list[list[float]]],
    gates: list[dict[str, Any]],
) -> tuple[set[str], list[str]]:
    changed = {
        sid for sid, session in after.items()
        if sid not in before or before[sid] != session
    }
    removed = [sid for sid in before if sid not in after]
    gate_by_id = {str(g["id"]): g for g in gates}

    # Existing reservations are invalidated by either a width mismatch or an
    # outage on any IFATC alternative in that passenger conflict group.
    for sid, gate_id in assignments.items():
        if sid not in after:
            continue
        gate = gate_by_id.get(gate_id)
        if gate is None or not allowed_width(after[sid].get("widthClass"), gate.get("widthClass")):
            changed.add(sid)
            continue
        group = str(gate.get("conflictGroup", ""))
        for start, end in outages.get(group, []):
            if float(after[sid]["start"]) < float(end) and float(start) < float(after[sid]["end"]):
                changed.add(sid)
                break

    return changed, removed

def allowed_width(aircraft_class: str | None, gate_class: str | None) -> bool:
    if aircraft_class not in WIDTH_CLASSES or gate_class not in WIDTH_CLASSES:
        return False
    # Crosswalk widthClass is the maximum mapped capacity class.
    return WIDTH_CLASSES.index(gate_class) >= WIDTH_CLASSES.index(aircraft_class)



def _earliest_gate_start(
    session: dict[str, Any],
    conflict_group: str,
    occupied: dict[str, list[tuple[float, float]]],
    outages: dict[str, list[list[float]]],
) -> float | None:
    """Return the earliest time this gate can hold the aircraft until departure."""
    session_start = float(session["start"])
    session_end = float(session["end"])
    candidate = session_start
    if candidate >= session_end:
        return None

    blocked = list(occupied.get(conflict_group, []))
    blocked.extend((float(a), float(b)) for a, b in outages.get(conflict_group, []))
    for block_start, block_end in sorted(blocked):
        # A booking/outage anywhere in the aircraft's remaining stay means
        # it must wait until that block ends before entering this gate.
        if candidate < block_end and block_start < session_end:
            candidate = max(candidate, block_end)
            if candidate >= session_end:
                return None
    return candidate if candidate < session_end else None


def repair_assignments_python(
    sessions: dict[str, dict[str, Any]],
    gates: list[dict[str, str]],
    outages: dict[str, list[list[float]]],
    previous: dict[str, str],
    changed: set[str],
    previous_gate_starts: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Repair assignments and queue otherwise-unassigned aircraft for a later gate."""
    previous_gate_starts = previous_gate_starts or {}
    by_id = sessions
    occupied: dict[str, list[tuple[float, float]]] = defaultdict(list)
    gate_by_id = {str(g["id"]): g for g in gates}
    for sid, gate_id in previous.items():
        if sid in changed or sid not in by_id or gate_id not in gate_by_id:
            continue
        session = by_id[sid]
        booking_start = float(previous_gate_starts.get(sid, session["start"]))
        group = str(gate_by_id[gate_id]["conflictGroup"])
        occupied[group].append((booking_start, float(session["end"])))

    gate_index = {str(g["id"]): i for i, g in enumerate(gates)}

    def candidates_for(session: dict[str, Any]) -> list[dict[str, Any]]:
        aircraft_class = session.get("widthClass")
        if aircraft_class not in WIDTH_CLASSES:
            return []
        result = []
        fixed_gate = session.get("fixedGate")
        for gate in gates:
            if fixed_gate and gate.get("id") != fixed_gate:
                continue
            if not allowed_width(aircraft_class, gate.get("widthClass")):
                continue
            result.append(gate)
        return result

    decision_ids = [sid for sid in changed if sid in by_id]
    candidate_map = {sid: candidates_for(by_id[sid]) for sid in decision_ids}
    decision_ids.sort(key=lambda sid: (
        0 if by_id[sid].get("initiallyParked", False) else 1,
        0 if by_id[sid].get("priority", False) else 1,
        len(candidate_map[sid]),
        0 if sid in previous else 1,
        sid,
    ))

    new_assignments: list[list[str]] = []
    reassignments: list[list[str]] = []
    retained_assignments: list[list[str]] = []
    unassigned: list[str] = []
    unassigned_reasons: dict[str, str] = {}
    gate_starts: dict[str, float] = {}
    waiting: dict[str, dict[str, Any]] = {}

    for sid in decision_ids:
        session = by_id[sid]
        old_gate = previous.get(sid)
        candidates = candidate_map[sid]
        start, end = float(session["start"]), float(session["end"])

        def cost(gate: dict[str, Any]) -> tuple[float, str]:
            same = session.get("widthClass") == gate.get("widthClass")
            value = 0.0 if same else 500.0
            if gate.get("kind") == "remote":
                value += 5000.0
            preferred = session.get("preferredTerminal")
            gate_terminal = gate.get("terminal")
            if preferred and gate_terminal and preferred != gate_terminal:
                value += 75.0  # soft carrier-terminal preference, never exclusivity
            value += gate_index[str(gate["id"])] * 0.001
            if old_gate is not None and str(gate["id"]) == old_gate:
                value -= 1_000_000.0
            return value, str(gate["id"])

        selected = None
        selected_start = None
        ranked = sorted(candidates, key=cost)

        # First try a gate available at the aircraft's scheduled arrival.
        for gate in ranked:
            gid = str(gate["id"])
            available = _earliest_gate_start(session, str(gate["conflictGroup"]), occupied, outages)
            if available is not None and available <= start:
                selected = gid
                selected_start = start
                break

        # Aircraft already parked at initialization cannot wait off-gate for
        # a future opening. They must receive a gate immediately or remain
        # explicitly unassigned as an initial-parking conflict.
        if selected is None and not session.get("initiallyParked", False):
            future_options = []
            for gate in ranked:
                gid = str(gate["id"])
                available = _earliest_gate_start(session, str(gate["conflictGroup"]), occupied, outages)
                if available is not None:
                    future_options.append((available, cost(gate), gid))
            if future_options:
                selected_start, _, selected = min(future_options)

        if selected is None:
            unassigned.append(sid)
            if not candidates:
                unassigned_reasons[sid] = "no_compatible_gate"
            elif session.get("initiallyParked", False):
                unassigned_reasons[sid] = "initially_parked_no_gate_available_at_start"
            else:
                unassigned_reasons[sid] = "no_gate_slot_before_departure"
            continue

        selected_group = str(gate_by_id[selected]["conflictGroup"])
        occupied[selected_group].append((float(selected_start), end))
        gate_starts[sid] = float(selected_start)
        if selected_start > start:
            waiting[sid] = {
                "gate": selected,
                "arrival_minute": start,
                "gate_entry_minute": float(selected_start),
                "wait_minutes": float(selected_start) - start,
            }
        if old_gate is None:
            new_assignments.append([sid, selected])
        elif selected != old_gate:
            reassignments.append([sid, selected])
        else:
            retained_assignments.append([sid, selected])

    return {
        "assignments": new_assignments,
        "reassignments": reassignments,
        "retained_assignments": retained_assignments,
        "unassigned": unassigned,
        "unassigned_reasons": unassigned_reasons,
        "gate_starts": gate_starts,
        "waiting": waiting,
    }

def build_solver_payload(
    sessions: dict[str, dict[str, Any]],
    gates: list[dict[str, str]],
    outages: dict[str, list[list[float]]],
) -> dict[str, Any]:
    return {
        "sessions": list(sessions.values()),
        "gates": gates,
        "outages": outages,
    }



# Process-wide matcher state. Requests are serialized because the updater's
# contract requires sequential state transitions.
STATE: ScenarioState | None = None
ASSIGNMENTS: dict[str, str] = {}
GATE_STARTS: dict[str, float] = {}
CURRENT_RUN_ID: str | None = None
LAST_EVENT_INDEX = -1
LAST_EVENT_ID: str | None = None
STATE_LOCK = threading.RLock()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _empty_response(handler: BaseHTTPRequestHandler, status: int = 204) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _parse_stream_headers(headers) -> tuple[str, int, str]:
    run_id = headers.get("X-Stream-Run-Id", "").strip()
    event_id = headers.get("X-Stream-Event-Id", "").strip()
    simulated_time = headers.get("X-Stream-Simulated-Time", "").strip()
    if not run_id or not event_id or not simulated_time:
        raise ValueError("Missing one or more required X-Stream-* headers.")
    prefix = run_id + ":"
    if not event_id.startswith(prefix):
        raise ValueError("X-Stream-Event-Id must be '<run-id>:<zero-based-index>'.")
    try:
        index = int(event_id[len(prefix):])
    except ValueError as exc:
        raise ValueError("X-Stream-Event-Id has a non-integer event index.") from exc
    if index < 0:
        raise ValueError("Event index must be non-negative.")
    # Validate UTC timestamp syntax; the original exact string is retained in logs.
    parsed = datetime.fromisoformat(simulated_time.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("X-Stream-Simulated-Time must be an offset-aware UTC timestamp.")
    return run_id, index, simulated_time


def _refresh_horizon(state: ScenarioState) -> None:
    if state.horizon_end is None:
        return
    latest = max(state._all_yyz_event_times(), default=state.horizon_end)
    state.horizon_end = max(state.horizon_end, latest + timedelta(hours=3))


def build_aircraft_status(
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_starts: dict[str, float],
    unassigned_reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Separate parked aircraft, gate-assigned arrivals, and waiting arrivals."""
    unassigned_reasons = unassigned_reasons or {}
    parked_aircraft: dict[str, dict[str, Any]] = {}
    assigned_aircraft: dict[str, dict[str, Any]] = {}
    waiting_sessions: dict[str, dict[str, Any]] = {}
    waiting_aircraft: dict[str, dict[str, Any]] = {}
    unassigned_sessions = sorted(sid for sid in sessions if sid not in assignments)
    unassigned_details: dict[str, dict[str, Any]] = {}

    for sid, session in sessions.items():
        start = float(session["start"])
        end = float(session["end"])
        gate_start = float(gate_starts.get(sid, start))
        base = {
            "aircraft": session.get("registration"),
            "aircraft_type": session.get("aircraftType"),
            "arrival_minute": start,
            "departure_minute": end,
        }
        if sid in assignments:
            record = {**base, "gate": assignments[sid], "gate_entry_minute": gate_start}
            if session.get("initiallyParked", False):
                parked_aircraft[sid] = {**record, "status": "parked_at_initialization"}
            elif gate_start > start:
                waiting_sessions[sid] = {
                    "gate": assignments[sid],
                    "arrival_minute": start,
                    "gate_entry_minute": gate_start,
                    "wait_minutes": gate_start - start,
                }
                waiting_aircraft[sid] = {
                    **record,
                    "status": "waiting_for_reserved_gate",
                    "wait_minutes": gate_start - start,
                }
            else:
                assigned_aircraft[sid] = {**record, "status": "gate_assigned"}
        else:
            default_reason = (
                "initially_parked_no_gate_available_at_start"
                if session.get("initiallyParked", False)
                else "no_feasible_gate_slot"
            )
            reason = unassigned_reasons.get(sid, default_reason)
            detail = {**base, "reason": reason}
            unassigned_details[sid] = detail
            if session.get("initiallyParked", False):
                parked_aircraft[sid] = {
                    **base,
                    "gate": None,
                    "status": "parked_at_initialization_without_gate",
                    "reason": reason,
                }
            else:
                waiting_aircraft[sid] = {
                    **base,
                    "gate": None,
                    "status": "waiting_without_gate_assignment",
                    "reason": reason,
                }

    return {
        "parked_aircraft": parked_aircraft,
        "assigned_aircraft": assigned_aircraft,
        "waiting_aircraft": waiting_aircraft,
        "waiting_sessions": waiting_sessions,
        "unassigned_sessions": unassigned_sessions,
        "unassigned_details": unassigned_details,
        # Compatibility field now contains only aircraft genuinely waiting for a gate.
        "waiting_for_gate": sorted(waiting_aircraft),
    }


LATEST_GATE_STATE: dict[str, Any] = {}
LATEST_UNASSIGNED_AIRCRAFT: list[dict[str, Any]] = []
LATEST_SIMULATED_TIME: str | None = None
LAST_SOLVER_SUMMARY: dict[str, Any] = {}
LAST_DECISION_RESPONSE: dict[str, Any] = {"assignments": [], "moves": []}
PASSENGER_CLIENTS: dict[Any, list[dict[str, Any]] | None] = {}
PASSENGER_CLIENT_LOCK = threading.RLock()
PASSENGER_LAST_FIELDS: dict[str, dict[str, Any]] = {}
PASSENGER_PREVIOUS_FIELDS: dict[str, dict[str, Any]] = {}
LAST_PASSENGER_FLIGHTS_JSON: str | None = None
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def active_token_legs(state: ScenarioState) -> dict[str, dict[str, Any]]:
    active = {}
    for route in state.routes.values():
        for entry in route["legs"]:
            if entry["active"]:
                active[entry["token"]] = state.token_legs.get(entry["token"], {
                    "carrier": route["carrier"], "leg": entry["leg"]
                })
    return active


def current_minute(state: ScenarioState) -> float:
    if not LATEST_SIMULATED_TIME:
        return 0.0
    local_now = local_event_time(LATEST_SIMULATED_TIME, state.airport_tz)
    return minutes_from_epoch(local_now, state.epoch)


def build_stream_decisions(
    state: ScenarioState,
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_starts: dict[str, float],
    session_ids: set[str] | None = None,
    *,
    include_all: bool = False,
    respect_gate_start: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Translate internal parking-session choices into the stream's exact contract."""
    selected = set(sessions) if include_all else set(session_ids or ())
    active_refs = active_token_legs(state)
    decision_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    move_map: dict[str, dict[str, str]] = {}
    occupied_now = {
        str(plane.get("registration")): gate
        for gate, plane in LATEST_GATE_STATE.items() if plane is not None
    }
    unassigned_now = {
        str(plane.get("registration"))
        for plane in LATEST_UNASSIGNED_AIRCRAFT if plane.get("registration")
    }
    now_minute = current_minute(state)

    for sid in selected:
        session = sessions.get(sid)
        gate = assignments.get(sid)
        if session is None or gate is None:
            continue
        start = float(session["start"])
        gate_start = float(gate_starts.get(sid, start))
        registration = str(session.get("registration", ""))
        is_grounded = registration in occupied_now or registration in unassigned_now
        is_current = is_grounded and start <= now_minute < float(session["end"])
        # The stream does not schedule delayed tow-in times. Do not send a gate
        # choice before its actual gate-entry time; send it when the aircraft is
        # on the ground and the reserved slot has become usable.
        effective_time = now_minute if is_current else start
        if respect_gate_start and gate_start > effective_time:
            continue

        for token in (session.get("start_token"), session.get("end_token")):
            if not token or token not in active_refs:
                continue
            reference = active_refs[token]
            leg = dict(reference.get("leg", {}))
            identity = (
                reference.get("carrier"),
                leg.get("flightNumber"),
                leg.get("flightSuffix"),
                leg.get("originatingDate"),
                leg.get("departureStation"),
                leg.get("arrivalStation"),
            )
            decision = {
                "carrier": reference["carrier"],
                "leg": {key: leg.get(key) for key in (
                    "flightNumber", "flightSuffix", "originatingDate",
                    "departureStation", "arrivalStation"
                )},
                "gate": gate,
            }
            previous = decision_map.get(identity)
            if previous is not None and previous["gate"] != gate:
                raise RuntimeError("One flight leg was assigned to two different passenger gates.")
            decision_map[identity] = decision

        # A currently grounded aircraft may need an immediate tow. Emit the
        # explicit move as well as leg assignments so sessions without a
        # matching active leg (e.g. idle parked aircraft) are still actionable.
        if is_current:
            current_gate = occupied_now.get(registration)
            if current_gate != gate:
                move_map[registration] = {"registration": registration, "gate": gate}

    return {
        "assignments": list(decision_map.values()),
        "moves": list(move_map.values()),
    }


def dashboard_snapshot() -> dict[str, Any]:
    if STATE is None:
        return {
            "status": "waiting_for_initialization",
            "run_id": CURRENT_RUN_ID,
            "last_event_id": LAST_EVENT_ID,
            "simulated_time": LATEST_SIMULATED_TIME,
            "assignments": [],
            "gate_assignments": [],
            "gate_state": {},
            "unassigned_aircraft": [],
            "gates": [],
        }

    sessions = STATE.build_sessions()
    status = build_aircraft_status(sessions, ASSIGNMENTS, GATE_STARTS)
    full = build_stream_decisions(
        STATE, sessions, ASSIGNMENTS, GATE_STARTS, include_all=True,
        respect_gate_start=False
    )
    occupants = {
        gate: plane for gate, plane in LATEST_GATE_STATE.items() if plane is not None
    }
    gates = []
    for gate in STATE.gates:
        gate_id = gate["id"]
        gates.append({
            "gate": gate_id,
            "terminal": gate.get("terminal"),
            "widthClass": gate.get("widthClass"),
            "conflictGroup": gate.get("conflictGroup"),
            "kind": gate.get("kind"),
            "occupant": occupants.get(gate_id),
            "outages": STATE.outage_payload().get(str(gate.get("conflictGroup")), []),
        })

    gate_assignments = []
    for sid, session in sessions.items():
        gate = ASSIGNMENTS.get(sid)
        gate_assignments.append({
            "session_id": sid,
            "registration": session.get("registration"),
            "carrier": session.get("carrier"),
            "aircraft_type": session.get("aircraftType"),
            "gate": gate,
            "arrival_minute": session.get("start"),
            "departure_minute": session.get("end"),
            "gate_entry_minute": GATE_STARTS.get(sid, session.get("start")),
            "status": ("unassigned" if gate is None else
                       "waiting_for_gate" if GATE_STARTS.get(sid, session.get("start")) > session.get("start")
                       else "assigned"),
            "traffic_types": session.get("trafficTypes", []),
            "preferred_terminal": session.get("preferredTerminal"),
        })

    return {
        "status": "ready",
        "run_id": CURRENT_RUN_ID,
        "last_event_id": LAST_EVENT_ID,
        "simulated_time": LATEST_SIMULATED_TIME,
        "assignments": full["assignments"],
        "gate_assignments": gate_assignments,
        "unassigned": status["unassigned_sessions"],
        "unassigned_details": status["unassigned_details"],
        "gate_state": copy.deepcopy(LATEST_GATE_STATE),
        "gate_state_source": "latest stream snapshot received before the current matcher decision",
        "unassigned_aircraft": copy.deepcopy(LATEST_UNASSIGNED_AIRCRAFT),
        "gates": gates,
        "outages": copy.deepcopy(STATE.outages),
        "solver": copy.deepcopy(LAST_SOLVER_SUMMARY),
        "last_decision_response": copy.deepcopy(LAST_DECISION_RESPONSE),
        "last_decision_timestamp": LATEST_SIMULATED_TIME,
    }


def _handle_initialize(message: dict[str, Any], simulated_time: str) -> dict[str, Any]:
    global STATE, ASSIGNMENTS, GATE_STARTS
    global LATEST_GATE_STATE, LATEST_UNASSIGNED_AIRCRAFT, LATEST_SIMULATED_TIME, LAST_SOLVER_SUMMARY
    global PASSENGER_LAST_FIELDS, PASSENGER_PREVIOUS_FIELDS, LAST_PASSENGER_FLIGHTS_JSON
    global LAST_DECISION_RESPONSE

    PASSENGER_LAST_FIELDS = {}
    PASSENGER_PREVIOUS_FIELDS = {}
    LAST_PASSENGER_FLIGHTS_JSON = None
    aircraft_classes, gates, airport_timezone = load_references()
    candidate = ScenarioState({}, aircraft_classes, gates, airport_timezone)
    candidate.initialize({"timestamp": simulated_time, "message": message})
    sessions = candidate.build_sessions()

    # The stream already has physical positions for parked aircraft. Lock
    # those positions for the initial solve; never silently reshuffle the
    # initialization snapshot.
    for session in sessions.values():
        if session.get("initiallyParked"):
            fixed = candidate.parked_gates.get(norm(session.get("registration")))
            if fixed:
                session["fixedGate"] = fixed

    payload = build_solver_payload(sessions, gates, candidate.outage_payload())
    payload["mode"] = "initial"
    result = run_julia(SOLVER_BACKEND, payload)
    assignments = assignment_map(result.get("assignments", []))
    gate_starts = {
        sid: float(sessions[sid]["start"])
        for sid in assignments if sid in sessions
    }

    # Keep the existing local repair fallback for sessions the joint model
    # cannot place. Conflict-group occupancy and crosswalk capacity are used
    # by the repair path too.
    initial_unassigned = [sid for sid in result.get("unassigned", []) if sid in sessions]
    waiting_result = repair_assignments_python(
        sessions, gates, candidate.outage_payload(), assignments,
        set(initial_unassigned), gate_starts,
    ) if initial_unassigned else {
        "assignments": [], "reassignments": [], "retained_assignments": [],
        "unassigned": [], "unassigned_reasons": {}, "gate_starts": {}, "waiting": {},
    }
    for pair in waiting_result.get("assignments", []):
        assignments[str(pair[0])] = str(pair[1])
    for pair in waiting_result.get("reassignments", []):
        assignments[str(pair[0])] = str(pair[1])
    gate_starts.update(waiting_result.get("gate_starts", {}))

    STATE = candidate
    ASSIGNMENTS = assignments
    GATE_STARTS = gate_starts
    LATEST_GATE_STATE = copy.deepcopy(message.get("gateState", {}))
    LATEST_UNASSIGNED_AIRCRAFT = copy.deepcopy(message.get("unassignedAircraft", []))
    LATEST_SIMULATED_TIME = simulated_time
    LAST_SOLVER_SUMMARY = {
        "mode": "initial",
        "termination_status": result.get("termination_status"),
        "objective": result.get("objective"),
        "sessions": len(sessions),
        "assigned_sessions": len(assignments),
        "unassigned_sessions": sorted(sid for sid in sessions if sid not in assignments),
    }

    decisions = build_stream_decisions(
        candidate, sessions, assignments, gate_starts, include_all=True
    )
    LAST_DECISION_RESPONSE = copy.deepcopy(decisions)
    return decisions


def _handle_update(message: dict[str, Any], simulated_time: str) -> dict[str, Any]:
    global STATE, ASSIGNMENTS, GATE_STARTS
    global LATEST_GATE_STATE, LATEST_UNASSIGNED_AIRCRAFT, LATEST_SIMULATED_TIME, LAST_SOLVER_SUMMARY
    global LAST_DECISION_RESPONSE

    if STATE is None or not STATE.initialized:
        raise RuntimeError("Matcher has not received InitializeAirport.")

    candidate = copy.deepcopy(STATE)
    before = candidate.build_sessions()
    action = candidate.apply_message({"timestamp": simulated_time, "message": message})
    candidate.validate_timezone_coverage()
    _refresh_horizon(candidate)
    after = candidate.build_sessions()
    outages = candidate.outage_payload()
    assignments = dict(ASSIGNMENTS)
    gate_starts = dict(GATE_STARTS)

    changed, removed = determine_changed_sessions(before, after, assignments, outages, candidate.gates)
    for sid in removed:
        assignments.pop(sid, None)
        gate_starts.pop(sid, None)
    for sid in list(assignments):
        if sid not in after:
            assignments.pop(sid, None)
            gate_starts.pop(sid, None)

    waiting_ids = {
        sid for sid, gate in assignments.items()
        if sid in after
        and float(gate_starts.get(sid, after[sid]["start"])) > float(after[sid]["start"])
    }
    unassigned_ids = {sid for sid in after if sid not in assignments}
    repair_ids = set(changed) | waiting_ids | unassigned_ids

    solver_result: dict[str, Any] = {
        "assignments": [], "reassignments": [], "retained_assignments": [],
        "unassigned": [], "unassigned_reasons": {}, "gate_starts": {}, "waiting": {},
    }
    if repair_ids:
        solver_result = repair_assignments_python(
            after, candidate.gates, outages, assignments, repair_ids, gate_starts
        )
        for sid in repair_ids:
            if sid not in after:
                assignments.pop(sid, None)
                gate_starts.pop(sid, None)
        for sid in repair_ids:
            assignments.pop(sid, None)
            gate_starts.pop(sid, None)
        for pair in solver_result.get("assignments", []):
            assignments[str(pair[0])] = str(pair[1])
        for pair in solver_result.get("reassignments", []):
            assignments[str(pair[0])] = str(pair[1])
        for pair in solver_result.get("retained_assignments", []):
            assignments[str(pair[0])] = str(pair[1])
        gate_starts.update(solver_result.get("gate_starts", {}))

    # Commit the candidate only after the complete schedule repair succeeds.
    STATE = candidate
    ASSIGNMENTS = assignments
    GATE_STARTS = gate_starts
    LATEST_GATE_STATE = copy.deepcopy(message.get("gateState", {}))
    LATEST_UNASSIGNED_AIRCRAFT = copy.deepcopy(message.get("unassignedAircraft", []))
    LATEST_SIMULATED_TIME = simulated_time

    decision_sessions = set()
    for key in ("assignments", "reassignments", "retained_assignments"):
        decision_sessions.update(str(pair[0]) for pair in solver_result.get(key, []))
    LAST_SOLVER_SUMMARY = {
        "mode": "repair",
        "action": action,
        "changed_sessions": sorted(changed),
        "reconsidered_waiting_sessions": sorted(waiting_ids),
        "removed_sessions": removed,
        "sessions": len(after),
        "assigned_sessions": len(assignments),
        "unassigned_sessions": sorted(sid for sid in after if sid not in assignments),
        "new_assignments": len(solver_result.get("assignments", [])),
        "reassignments": len(solver_result.get("reassignments", [])),
    }
    decisions = build_stream_decisions(
        candidate, after, assignments, gate_starts, decision_sessions
    )
    LAST_DECISION_RESPONSE = copy.deepcopy(decisions)
    return decisions

def _passenger_gate_object(gate_id: str | None) -> dict[str, str] | None:
    if not gate_id or STATE is None:
        return None
    record = STATE.gate_by_id.get(gate_id)
    if record is None:
        return None
    terminal = record.get("terminal")
    if terminal is None and record.get("kind") == "remote":
        terminal = "Remote"
    if terminal is None:
        return None
    terminal_label = str(terminal)
    if terminal_label.startswith("T") and terminal_label[1:].isdigit():
        terminal_label = terminal_label[1:]
    return {"terminal": terminal_label, "gate": str(gate_id)}


def build_passenger_message() -> dict[str, Any]:
    """Build the passenger branch's documented full-list WebSocket payload."""
    global PASSENGER_LAST_FIELDS, PASSENGER_PREVIOUS_FIELDS
    if STATE is None:
        return {"type": "assignments", "generatedAt": datetime.now(timezone.utc).isoformat(), "flights": []}

    sessions = STATE.build_sessions()
    gate_by_token: dict[str, str] = {}
    for sid, session in sessions.items():
        gate = ASSIGNMENTS.get(sid)
        if gate:
            for token in (session.get("start_token"), session.get("end_token")):
                if token:
                    gate_by_token[token] = gate

    now_utc = (datetime.fromisoformat(LATEST_SIMULATED_TIME.replace("Z", "+00:00"))
               if LATEST_SIMULATED_TIME else datetime.now(timezone.utc))
    flights = []
    for route in STATE.routes.values():
        carrier = route["carrier"]
        for entry in route["legs"]:
            leg = entry["leg"]
            try:
                dep_yyz, arr_yyz = STATE._leg_datetimes(leg)
            except (KeyError, ValueError, TimezoneCoverageError):
                continue
            if dep_yyz is None and arr_yyz is None:
                continue

            cancelled = not entry["active"]
            if cancelled:
                cancelled_at = entry.get("cancelledAt")
                if not cancelled_at:
                    continue
                cancelled_time = datetime.fromisoformat(cancelled_at.replace("Z", "+00:00"))
                if now_utc - cancelled_time > timedelta(hours=1):
                    continue
            else:
                # Keep completed YYZ movements on the board for roughly one hour.
                last_movement = max(
                    [value for value in (dep_yyz, arr_yyz) if value is not None],
                    default=None,
                )
                if last_movement is not None:
                    last_movement_utc = last_movement.replace(
                        tzinfo=STATE.airport_tz
                    ).astimezone(timezone.utc)
                    if now_utc - last_movement_utc > timedelta(hours=1):
                        continue

            token = entry["token"]
            gate = _passenger_gate_object(gate_by_token.get(token))
            number = str(leg.get("flightNumber") or "")
            suffix = str(leg.get("flightSuffix") or "")
            primary = f"{carrier}{number}{suffix}"
            codes = [primary] if primary else []
            for share in leg.get("codeshare", []):
                share_code = str(share.get("carrier") or "") + str(share.get("flightNumber") or "")
                if share_code and share_code not in codes:
                    codes.append(share_code)

            time_departure = (
                dep_yyz.replace(tzinfo=STATE.airport_tz).isoformat() if dep_yyz is not None else None
            )
            time_arrival = (
                arr_yyz.replace(tzinfo=STATE.airport_tz).isoformat() if arr_yyz is not None else None
            )
            fields = {
                "gate": gate,
                "timeArrival": time_arrival,
                "timeDeparture": time_departure,
                "cancelled": cancelled,
            }
            previous = copy.deepcopy(PASSENGER_PREVIOUS_FIELDS.get(token, {}))
            old = PASSENGER_LAST_FIELDS.get(token)
            if old is not None:
                for key, value in fields.items():
                    if old.get(key) != value:
                        previous[key] = copy.deepcopy(old.get(key))
            PASSENGER_PREVIOUS_FIELDS[token] = previous
            PASSENGER_LAST_FIELDS[token] = copy.deepcopy(fields)

            flights.append({
                "flightId": token,
                "flightCodes": codes or [carrier],
                "airline": AIRLINE_NAMES.get(carrier, carrier),
                "from": str(leg.get("departureStation") or ""),
                "to": str(leg.get("arrivalStation") or ""),
                "timeArrival": time_arrival,
                "timeDeparture": time_departure,
                "gate": gate,
                "cancelled": cancelled,
                "previous": previous,
                "misc": {
                    "aircraftType": entry["aircraft"].get("aircraftType"),
                    "registration": entry["aircraft"].get("registration"),
                    "trafficType": leg.get("trafficType"),
                },
            })

    def flight_sort_key(flight):
        value = flight.get("timeDeparture") or flight.get("timeArrival")
        return datetime.fromisoformat(value).timestamp() if value else float("inf")

    flights.sort(key=flight_sort_key)
    generated = now_utc.astimezone(STATE.airport_tz).isoformat()
    return {"type": "assignments", "generatedAt": generated, "flights": flights}


def parse_passenger_gate_selectors(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw or not raw.strip():
        return None
    selectors = []
    range_pattern = re.compile(r"^([A-Z]+)(\d+)-(?:([A-Z]+))?(\d+)$")
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if ":" in value:
            terminal, target = value.split(":", 1)
            target = target.upper()
        else:
            terminal, target = value, ""
        if not target:
            selectors.append({"terminal": terminal})
            continue
        match = range_pattern.fullmatch(target)
        if match:
            branch, first, end_branch, last = match.groups()
            if end_branch and end_branch != branch:
                continue
            selectors.append({
                "terminal": terminal, "branch": branch,
                "from": min(int(first), int(last)), "to": max(int(first), int(last)),
            })
        elif re.fullmatch(r"[A-Z]+", target):
            selectors.append({"terminal": terminal, "branch": target})
        else:
            selectors.append({"terminal": terminal, "gate": target})
    return selectors


def passenger_gate_matches(gate: dict[str, Any] | None, selector: dict[str, Any]) -> bool:
    if not gate or str(gate.get("terminal")) != str(selector["terminal"]):
        return False
    label = str(gate.get("gate", "")).upper()
    if "gate" in selector:
        return label == selector["gate"]
    branch_match = re.match(r"^[A-Z]+", label)
    branch = branch_match.group(0) if branch_match else ""
    if "from" in selector:
        number_match = re.search(r"\d+", label)
        if branch != selector["branch"] or not number_match:
            return False
        number = int(number_match.group(0))
        return selector["from"] <= number <= selector["to"]
    if "branch" in selector:
        return branch == selector["branch"]
    return True


def filter_passenger_flights(flights: list[dict[str, Any]], selectors: list[dict[str, Any]] | None):
    if selectors is None:
        return flights
    result = []
    for flight in flights:
        previous_gate = flight.get("previous", {}).get("gate")
        if any(passenger_gate_matches(flight.get("gate"), item)
               or passenger_gate_matches(previous_gate, item) for item in selectors):
            result.append(flight)
    return result


def _ws_send(sock, payload: bytes, opcode: int = 1) -> None:
    length = len(payload)
    head = bytes([0x80 | opcode])
    if length < 126:
        head += bytes([length])
    elif length < 65536:
        head += bytes([126]) + struct.pack("!H", length)
    else:
        head += bytes([127]) + struct.pack("!Q", length)
    sock.sendall(head + payload)


def _ws_read_exact(sock, length: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < length:
        part = sock.recv(length - len(chunks))
        if not part:
            return None
        chunks.extend(part)
    return bytes(chunks)


def _ws_read_frame(sock):
    header = _ws_read_exact(sock, 2)
    if header is None:
        return None, b""
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        extended = _ws_read_exact(sock, 2)
        if extended is None:
            return None, b""
        length = struct.unpack("!H", extended)[0]
    elif length == 127:
        extended = _ws_read_exact(sock, 8)
        if extended is None:
            return None, b""
        length = struct.unpack("!Q", extended)[0]
    masked = bool(header[1] & 0x80)
    mask = _ws_read_exact(sock, 4) if masked else None
    payload = _ws_read_exact(sock, length) if length else b""
    if payload is None:
        return None, b""
    if masked and mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def broadcast_passenger_assignments(*, force: bool = False) -> None:
    global LAST_PASSENGER_FLIGHTS_JSON
    if STATE is None:
        return
    message = build_passenger_message()
    signature = json.dumps(message["flights"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    if not force and signature == LAST_PASSENGER_FLIGHTS_JSON:
        return
    LAST_PASSENGER_FLIGHTS_JSON = signature
    with PASSENGER_CLIENT_LOCK:
        clients = list(PASSENGER_CLIENTS.items())
    for sock, selectors in clients:
        try:
            outgoing = {
                **message,
                "flights": filter_passenger_flights(message["flights"], selectors),
            }
            _ws_send(sock, json.dumps(outgoing, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except OSError:
            with PASSENGER_CLIENT_LOCK:
                PASSENGER_CLIENTS.pop(sock, None)




class MatcherHandler(BaseHTTPRequestHandler):
    server_version = "SmartGateMatcher/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _serve_passenger_websocket(self, query: str) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            _json_response(self, 426, {"error": "WebSocket upgrade required"})
            return
        if self.headers.get("Sec-WebSocket-Version") != "13":
            _json_response(self, 400, {"error": "WebSocket version 13 is required"})
            return
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if not key:
            _json_response(self, 400, {"error": "Missing Sec-WebSocket-Key"})
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        selectors = parse_passenger_gate_selectors(parse_qs(query).get("gates", [None])[0])
        sock = self.connection
        try:
            # Serialize initial snapshot + registration with stream updates so
            # a client cannot miss an update between its snapshot and subscribe.
            with STATE_LOCK:
                message = build_passenger_message()
                with PASSENGER_CLIENT_LOCK:
                    PASSENGER_CLIENTS[sock] = selectors
                    outgoing = {
                        **message,
                        "flights": filter_passenger_flights(message["flights"], selectors),
                    }
                    _ws_send(sock, json.dumps(outgoing, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            while True:
                opcode, payload = _ws_read_frame(sock)
                if opcode is None:
                    break
                if opcode == 8:
                    _ws_send(sock, payload, opcode=8)
                    break
                if opcode == 9:
                    _ws_send(sock, payload, opcode=10)
        except (ConnectionError, OSError):
            pass
        finally:
            with PASSENGER_CLIENT_LOCK:
                PASSENGER_CLIENTS.pop(sock, None)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/passenger/ws":
            self._serve_passenger_websocket(parsed.query)
            return
        if self.path == "/health":
            with STATE_LOCK:
                _json_response(self, 200, {
                    "status": "ready" if STATE is not None else "waiting_for_initialization",
                    "run_id": CURRENT_RUN_ID,
                    "last_event_id": LAST_EVENT_ID,
                })
            return
        if self.path == "/assignments":
            with STATE_LOCK:
                _json_response(self, 200, dashboard_snapshot())
            return
        _json_response(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:
        global CURRENT_RUN_ID, LAST_EVENT_INDEX, LAST_EVENT_ID
        if self.path != "/updates":
            _json_response(self, 404, {"error": "POST endpoint must be /updates"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000_000:
                raise ValueError("Request body is empty or exceeds 10 MB.")
            raw = self.rfile.read(length)
            message = json.loads(raw.decode("utf-8"))
            if not isinstance(message, dict) or not isinstance(message.get("action"), str):
                raise ValueError("Request body must be a JSON message object with an action.")
            run_id, index, simulated_time = _parse_stream_headers(self.headers)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _json_response(self, 400, {"error": str(exc)})
            return

        with STATE_LOCK:
            action = message["action"]
            try:
                if action == "InitializeAirport":
                    if index != 0:
                        raise ValueError("InitializeAirport must be event index 0.")
                    summary = _handle_initialize(message, simulated_time)
                    CURRENT_RUN_ID = run_id
                    LAST_EVENT_INDEX = 0
                    LAST_EVENT_ID = self.headers.get("X-Stream-Event-Id")
                    broadcast_passenger_assignments(force=True)
                    _json_response(self, 200, summary)
                    return

                if STATE is None:
                    raise RuntimeError("First message must be InitializeAirport.")
                if run_id != CURRENT_RUN_ID:
                    raise RuntimeError("Run ID changed without a new InitializeAirport.")
                if index != LAST_EVENT_INDEX + 1:
                    raise RuntimeError(
                        f"Out-of-order event index: expected {LAST_EVENT_INDEX + 1}, received {index}."
                    )
                summary = _handle_update(message, simulated_time)
                LAST_EVENT_INDEX = index
                LAST_EVENT_ID = self.headers.get("X-Stream-Event-Id")
                broadcast_passenger_assignments()
                print(json.dumps({
                    "event_id": LAST_EVENT_ID,
                    "simulated_time": simulated_time,
                    **summary,
                }, separators=(",", ":")), flush=True)
                _json_response(self, 200, summary)
            except TimezoneCoverageError as exc:
                # Bad/incomplete schedule reference data is a client validation
                # problem, not an internal server error. Keep event sequencing
                # unchanged so the sender can correct the data and retry.
                print(f"TIMEZONE COVERAGE event={self.headers.get('X-Stream-Event-Id', '?')}: {exc}", file=sys.stderr, flush=True)
                _json_response(self, 422, {"error": str(exc), "error_type": "timezone_coverage"})
            except Exception as exc:
                print(f"ERROR event={self.headers.get('X-Stream-Event-Id', '?')}: {exc}", file=sys.stderr, flush=True)
                _json_response(self, 500, {"error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartGate stream webhook matcher")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000, help="HTTP port (default: 9000)")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MatcherHandler)
    print(f"SmartGate matcher listening on http://{args.host}:{args.port}/updates", flush=True)
    print(f"Health: http://{args.host}:{args.port}/health | assignments: http://{args.host}:{args.port}/assignments", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping matcher.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
