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
import copy
import json
import os
import sys
import threading
import traceback
import queue
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any
from urllib.parse import urlsplit

from zoneinfo import ZoneInfo

from matcher_views import ENDPOINTS as VIEW_ENDPOINTS, SimClock, ViewFeed, ViewSnapshot

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
# The initial plan is always solved by Gurobi. A full day takes ~40 s, longer than
# the stream's 10 s per attempt: the stream retries InitializeAirport and receives
# the cached answer once the solve commits.
GUROBI_TIME_LIMIT = float(os.environ.get("GMS_GUROBI_TIME_LIMIT", "60"))
GUROBI_THREADS = int(os.environ.get("GMS_GUROBI_THREADS", str(os.cpu_count() or 1)))
# Covers JuMP model building on top of Gurobi's own limit.
JULIA_TIMEOUT = float(os.environ.get("GMS_JL_TIMEOUT", str(GUROBI_TIME_LIMIT + 60)))
WIDTH_CLASSES = ("A", "B", "C", "D", "E", "F")

# Assignment policy weights. Walking classes are ordinal effort scores from
# pearson.json, not measured metres or minutes.
MAX_TARMAC_WAIT_MINUTES = float(os.environ.get("GMS_MAX_TARMAC_WAIT_MINUTES", "60"))
WAITING_PENALTY_PER_MINUTE = float(os.environ.get("GMS_WAITING_PENALTY_PER_MINUTE", "1.0"))
WALKING_DISTANCE_PENALTY_PER_CLASS = float(os.environ.get("GMS_WALKING_PENALTY_PER_CLASS", "10.0"))
DOMESTIC_TO_INTERNATIONAL_PENALTY = float(os.environ.get("GMS_DOMESTIC_TO_INTERNATIONAL_PENALTY", "1000.0"))
EMERGENCY_GATE_PENALTY = float(os.environ.get("GMS_EMERGENCY_GATE_PENALTY", "1000.0"))
# Cost of towing an aircraft that is already at a gate. Below the remote-stand
# penalty, so an aircraft parked remotely still moves in when a terminal gate frees.
TOW_PENALTY = float(os.environ.get("GMS_TOW_PENALTY", "2000.0"))

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
    walking_class_by_gate: dict[str, int] = {}
    traffic_types_by_gate: dict[str, list[str]] = {}
    gate_zones = pearson_data.get("gateZones", {}).get("terminals", {})
    for terminal, terminal_data in pearson_data.get("passengerGateGroups", {}).get("terminals", {}).items():
        walking_by_label = terminal_data.get("walkingDistanceClassByGate", {})
        for group in terminal_data.get("groups", []):
            if not group:
                continue
            gate_id = str(group[0]).strip()
            terminal_by_gate[gate_id] = str(terminal)
            allowed_types: set[str] = set()
            walking_values = []
            for alias in group:
                alias = str(alias).strip()
                zone = alias[:1]
                allowed_types.update(gate_zones.get(str(terminal), {}).get(zone, []))
                value = walking_by_label.get(alias)
                if value is not None:
                    walking_values.append(int(value))
            if walking_values:
                walking_class_by_gate[gate_id] = min(walking_values)
            traffic_types_by_gate[gate_id] = sorted(allowed_types)

    airline_terminals = {}
    for row in pearson_data.get("airlineTerminals", {}).get("records", []):
        if not row.get("carrier") or row.get("terminal") is None:
            continue
        terminal_value = str(row["terminal"]).strip()
        airline_terminals[norm(row["carrier"])] = (
            f"T{terminal_value}" if terminal_value.isdigit() else terminal_value
        )

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
            "walkingDistanceClass": walking_class_by_gate.get(
                gate_id, 10 if gate_id.startswith("Remote Stand H") else 10
            ),
            "allowedTrafficTypes": traffic_types_by_gate.get(gate_id, []),
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
        self.snapshot_time: datetime | None = None
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

        self.snapshot_time = local_event_time(event["timestamp"], self.airport_tz)
        self.epoch = self.snapshot_time.replace(
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
            for leg_reference in legs:
                leg_found = self._find_route_leg(carrier, leg_reference)
                if leg_found is None:
                    continue
                _, _, updated_entry = leg_found
                updated = dict(leg_reference)
                if "legType" not in updated:
                    updated["legType"] = updated_entry["leg"].get("legType", "Originating")
                updated_entry["leg"] = updated
                self.token_legs[updated_entry["token"]] = {
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
                    updated_entry["aircraft"] = dict(flight)

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
            new_plane = dict(flight)
            if scope == "ALLLEGS":
                for item in self.routes[route_id]["legs"]:
                    item["aircraft"] = new_plane.copy()
            else:
                entry["aircraft"] = new_plane.copy()

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
            "untilFurtherNotice": not end_raw,
            "reason": message.get("reason"),
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

    def leg_local_datetimes(self, leg: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        """Return each end's aware datetime in its own station's time zone, for the dashboards."""
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
                f"YYZ-relevant route {dep_station}->{arr_station}."
            )
        dep_local = datetime.combine(
            day, parse_hhmm(leg["scheduledDepartureTime"])
        ).replace(tzinfo=dep_tz)
        dep_utc = dep_local.astimezone(timezone.utc)
        arr_clock = parse_hhmm(leg["scheduledArrivalTime"])
        arrival_candidates = [
            datetime.combine(day + timedelta(days=offset), arr_clock).replace(tzinfo=arr_tz)
            for offset in range(0, 3)
        ]
        valid_arrivals = [
            candidate for candidate in arrival_candidates
            if candidate.astimezone(timezone.utc) > dep_utc
        ]
        if not valid_arrivals:
            raise RuntimeError(f"Could not determine arrival date for {dep_station}->{arr_station}.")
        arr_local = min(valid_arrivals, key=lambda candidate: candidate.astimezone(timezone.utc))
        return dep_local, arr_local

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

    def build_sessions(self, *, gate_state: dict[str, Any] | None = None,
                       simulated_time: str | None = None) -> dict[str, dict[str, Any]]:
        if self.epoch is None or self.horizon_end is None:
            raise RuntimeError("Scenario state has not been initialized.")

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
            future_events = events

            if parked_aircraft:
                snapshot = self.snapshot_time or self.epoch
                past_events = [item for item in events if item["time"] <= snapshot]
                future_events = [item for item in events if item["time"] > snapshot]
                arrivals = [item for item in past_events if item["kind"] == "ARRIVE"]
                departures = [item for item in past_events if item["kind"] == "DEPART"]
                last_arrival = max(arrivals, key=lambda item: item["time"], default=None)
                last_departure = max(departures, key=lambda item: item["time"], default=None)

                if last_arrival is not None and (
                    last_departure is None or last_arrival["time"] > last_departure["time"]
                ):
                    current_start = max(self.epoch, last_arrival["time"])
                    current_start_token = last_arrival["token"]
                    current_aircraft = last_arrival["aircraft"]
                else:
                    current_start = self.epoch
                    current_start_token = None
                    current_aircraft = parked_aircraft

                first_future_departure = next(
                    (item for item in future_events if item["kind"] == "DEPART"), None
                )
                meta = first_future_departure["aircraft"] if first_future_departure else current_aircraft
                opened = (
                    current_start, f"PARKED:{registration}", meta, current_start_token, []
                )

            for item in future_events:
                if item["kind"] == "ARRIVE":
                    if opened is None:
                        opened = (item["time"], item["token"], item["aircraft"], item["token"], [])
                    # Cancellation can merge visits; every arrival needs the resulting gate plan.
                    opened[4].append(item["token"])
                elif opened is not None:
                    start_dt, anchor, meta, start_token, arrival_tokens = opened
                    if item["time"] > start_dt:
                        self._add_session(
                            sessions, anchor, registration, item["aircraft"],
                            start_dt, item["time"], start_token=start_token,
                            end_token=item["token"], arrival_tokens=arrival_tokens,
                        )
                    opened = None

            if opened is not None:
                start_dt, anchor, meta, start_token, arrival_tokens = opened
                if horizon > start_dt:
                    self._add_session(
                        sessions, anchor, registration, meta, start_dt, horizon,
                        start_token=start_token, end_token=None, arrival_tokens=arrival_tokens,
                    )

        if gate_state is not None:
            self._reserve_occupants(sessions, gate_state, simulated_time)
        return sessions

    def _reserve_occupants(self, sessions, gate_state, simulated_time):
        now = local_event_time(simulated_time, self.airport_tz)
        minute = minutes_from_epoch(now, self.epoch)
        seen_registrations, seen_groups = set(), set()
        for gate_id, plane in gate_state.items():
            if plane is None:
                continue
            gate = self.gate_by_id.get(gate_id)
            if gate is None:
                raise RuntimeError(f"Unknown occupied passenger gate {gate_id!r}.")
            registration = norm(plane.get("registration"))
            group = str(gate["conflictGroup"])
            if not registration or registration in seen_registrations or group in seen_groups:
                raise RuntimeError("Physical occupancy contains a duplicate aircraft or conflict group.")
            seen_registrations.add(registration)
            seen_groups.add(group)
            current = [s for s in sessions.values() if s["registration"] == registration
                       and float(s["start"]) <= minute < float(s["end"])]
            if current:
                session = max(current, key=lambda s: float(s["start"]))
            else:
                departures = []
                for route in self.routes.values():
                    for entry in route["legs"]:
                        if entry["active"] and norm(entry["aircraft"].get("registration")) == registration:
                            departure, _ = self._leg_datetimes(entry["leg"])
                            if departure is not None and departure > now:
                                departures.append((departure, entry["token"]))
                horizon = self.horizon_end if self.horizon_end > now else now + timedelta(days=1)
                end, end_token = min(departures, default=(horizon, None))
                sid = f"OCCUPIED:{registration}"
                self._add_session(sessions, sid, registration, plane, now, end, end_token=end_token)
                session = sessions[sid]
            # Flight equipment changes do not change the aircraft at a physical gate.
            session.update(carrier=norm(plane.get("carrier")), aircraftType=norm(plane.get("aircraftType")),
                           widthClass=self.aircraft_classes.get(norm(plane.get("aircraftType"))),
                           start=minute, occupiedGate=gate_id)

    def _add_session(self, sessions, anchor, registration, aircraft, start, end,
                     start_token=None, end_token=None, arrival_tokens=None):
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
        priority = bool(
            emergency_state == "Emergency"
            or (is_diversion and not remote_allowed)
            or turnback
        )
        session_start_token = start_token if start_token is not None else (
            None if anchor.startswith(("PARKED:", "OCCUPIED:")) else anchor
        )
        arrival_traffic_type = (
            self.token_legs.get(session_start_token, {}).get("leg", {}).get("trafficType")
            if session_start_token else None
        )
        departure_traffic_type = (
            self.token_legs.get(end_token, {}).get("leg", {}).get("trafficType")
            if end_token else None
        )
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
            "start_token": session_start_token,
            "end_token": end_token,
            "leg_tokens": [token for token in dict.fromkeys([*(arrival_tokens or []), session_start_token, end_token]) if token],
            "arrivalTrafficType": arrival_traffic_type,
            "departureTrafficType": departure_traffic_type,
            "preferredTerminal": self.airline_terminals.get(norm(aircraft.get("carrier"))),
            "trafficTypes": sorted({
                str(self.token_legs[token]["leg"].get("trafficType"))
                for token in (start_token or (None if anchor.startswith("PARKED:") else anchor), end_token)
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



WARMUP_PAYLOAD = {
    "mode": "initial",
    "sessions": [{"id": "warmup", "widthClass": "C", "start": 0, "end": 1}],
    "gates": [{"id": "warmup-gate", "conflictGroup": "warmup-group", "widthClass": "C", "kind": "gate"}],
    "outages": {},
}


class JuliaWorker:
    """Keeps one Julia process alive and sends it one JSON request at a time.

    Startup blocks until Julia is ready. A process lost later (timeout, crash) is
    restarted in the background; requests made meanwhile fail fast so callers can
    fall back instead of waiting out Julia's package loading.
    """

    def __init__(self, backend: Path, julia_executable: str, timeout: float):
        self.backend = backend
        self.julia_executable = julia_executable
        self.timeout = timeout
        self.process = None
        self.responses = None
        self.lock = threading.Lock()
        self.restarting = False
        self.closed = False
        try:
            self.start()
        except BaseException:
            self.close()
            raise

    @property
    def status(self) -> str:
        if self.process is not None and self.process.poll() is None:
            return "ready"
        return "restarting" if self.restarting else "unavailable"

    def _read_stdout(self, process, responses):
        try:
            for line in process.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    def _discard_process(self) -> None:
        """Kill the current process and start a replacement in the background."""
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if self.restarting or self.closed:
            return
        self.restarting = True

        def restart():
            try:
                self.start()
            except Exception as exc:
                print(f"Julia solver restart failed: {exc}", file=sys.stderr, flush=True)
            finally:
                self.restarting = False

        threading.Thread(target=restart, name="julia-restart", daemon=True).start()

    def start(self):
        if not self.backend.is_file():
            raise FileNotFoundError(
                f"Julia backend not found: {self.backend}"
            )

        try:
            process = subprocess.Popen(
                [self.julia_executable, str(self.backend)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(HERE),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Julia executable {self.julia_executable!r} "
                "was not found. Check that Julia is installed and on PATH."
            ) from exc

        responses = queue.Queue()

        reader = threading.Thread(
            target=self._read_stdout,
            args=(process, responses),
            daemon=True,
        )
        reader.start()

        # Wait for Julia to finish loading packages and announce readiness.
        try:
            line = responses.get(timeout=120)
        except queue.Empty:
            process.kill()
            process.wait()
            raise RuntimeError(
                "Julia did not become ready within 120 seconds."
            )

        if line is None:
            return_code = process.poll()
            raise RuntimeError(
                f"Julia exited during startup. Exit code: {return_code}"
            )

        try:
            ready_message = json.loads(line)
        except json.JSONDecodeError as exc:
            process.kill()
            process.wait()
            raise RuntimeError(
                f"Unexpected Julia startup response: {line!r}"
            ) from exc

        if ready_message.get("ready") is not True:
            process.kill()
            process.wait()
            raise RuntimeError(
                f"Julia did not send its ready signal: {ready_message}"
            )

        # Compile the solve path now, not during the first webhook's time budget.
        try:
            process.stdin.write(json.dumps(WARMUP_PAYLOAD) + "\n")
            process.stdin.flush()
            while True:
                line = responses.get(timeout=120)
                if line is None:
                    raise RuntimeError(f"Julia exited during warm-up. Exit code: {process.poll()}")
                if line.startswith("{") and ('"assignments"' in line or '"error"' in line):
                    break
        except (queue.Empty, OSError, RuntimeError) as exc:
            process.kill()
            process.wait()
            raise RuntimeError(f"Julia warm-up solve failed: {exc}") from exc

        with self.lock:
            if self.closed:
                process.kill()
                process.wait()
                return
            self.process, self.responses = process, responses
        print("Persistent Julia solver is ready.", flush=True)

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            allow_nan=False,
        )

        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._discard_process()
                raise RuntimeError("Julia solver is not running; a restart is in progress.")

            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._discard_process()
                raise RuntimeError(
                    "Could not send the request to the Julia solver."
                ) from exc

            # Gurobi may print informational messages to stdout.
            # Ignore those lines and wait for the actual JSON response.
            # The timeout covers the whole request; each log line must not extend it.
            deadline = _monotonic() + self.timeout
            while True:
                try:
                    line = self.responses.get(timeout=max(0.0, deadline - _monotonic()))
                except queue.Empty:
                    # A late answer would be read as the next request's, so the
                    # process cannot be reused.
                    self._discard_process()
                    raise RuntimeError(
                        f"Julia solver exceeded the "
                        f"{self.timeout:g}-second timeout."
                    )

                if line is None:
                    return_code = self.process.poll()
                    self._discard_process()
                    raise RuntimeError(
                        f"Julia solver exited unexpectedly. "
                        f"Exit code: {return_code}"
                    )

                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"Julia/Gurobi diagnostic: {line.strip()}",
                        flush=True,
                    )
                    continue

                if not isinstance(result, dict):
                    continue

                if "assignments" in result or "error" in result:
                    break

            if "error" in result:
                raise RuntimeError(
                    f"Julia solver error: {result['error']}"
                )

            if "assignments" not in result:
                raise RuntimeError(
                    f"Julia response is missing assignments: {result}"
                )

            return result

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is None:
                return

            process = self.process
            self.process = None

            if process.poll() is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                    process.wait()


JULIA_WORKER = None


def run_julia(backend: Path, payload: dict[str, Any]) -> dict[str, Any]:
    global JULIA_WORKER

    if not backend.is_file():
        raise FileNotFoundError(
            f"Julia backend not found: {backend}"
        )

    if JULIA_WORKER is None:
        raise RuntimeError(
            "Persistent Julia worker has not been started."
        )

    return JULIA_WORKER.run(payload)



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


def _traffic_compatible(session: dict[str, Any], gate: dict[str, Any]) -> bool:
    arrival = str(session.get("arrivalTrafficType") or "")
    departure = str(session.get("departureTrafficType") or "")

    if arrival == "International" and departure == "Domestic":
        return False

    required = {value for value in (arrival, departure) if value}
    allowed = set(gate.get("allowedTrafficTypes") or [])
    return not required or not allowed or required.issubset(allowed)


def _gate_assignment_cost(session: dict[str, Any], gate: dict[str, Any], index: int) -> float:
    aircraft_class = session.get("widthClass")
    gate_class = gate.get("widthClass")
    value = 0.0 if aircraft_class == gate_class else 500.0

    is_remote = gate.get("kind") == "remote"
    if is_remote:
        value += 5000.0
        if session.get("remoteAllowed", False):
            value += EMERGENCY_GATE_PENALTY

    preferred = session.get("preferredTerminal")
    gate_terminal = gate.get("terminal")
    if preferred and gate_terminal and preferred != gate_terminal:
        value += 75.0

    value += float(gate.get("walkingDistanceClass", 10)) * WALKING_DISTANCE_PENALTY_PER_CLASS

    if (
        session.get("arrivalTrafficType") == "Domestic"
        and session.get("departureTrafficType") == "International"
    ):
        value += DOMESTIC_TO_INTERNATIONAL_PENALTY

    return value + index * 0.001


def repair_assignments_python(
    sessions: dict[str, dict[str, Any]],
    gates: list[dict[str, Any]],
    outages: dict[str, list[list[float]]],
    previous: dict[str, str],
    changed: set[str],
    previous_gate_starts: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Repair changed sessions, allowing <=60 minutes of tarmac wait then overflow."""
    previous_gate_starts = previous_gate_starts or {}
    by_id = sessions
    changed = set(changed)
    mandatory = {sid for sid, session in sessions.items()
                 if session.get("occupiedGate") or session.get("fixedGate")}
    occupied: dict[str, list[tuple[float, float]]] = defaultdict(list)
    gate_by_id = {str(g["id"]): g for g in gates}

    gate_index = {str(g["id"]): i for i, g in enumerate(gates)}

    def candidates_for(session: dict[str, Any], include_remote: bool = False) -> list[dict[str, Any]]:
        aircraft_class = session.get("widthClass")
        if aircraft_class not in WIDTH_CLASSES:
            return []
        fixed_gate = session.get("fixedGate")
        result = []
        for gate in gates:
            gate_id = str(gate.get("id", ""))
            is_remote = gate.get("kind") == "remote"
            if fixed_gate and gate_id != fixed_gate:
                continue
            if is_remote and not include_remote:
                continue
            if (is_remote and session.get("initiallyParked", False)
                    and gate_id != (fixed_gate or session.get("occupiedGate"))):
                continue
            if not allowed_width(aircraft_class, gate.get("widthClass")):
                continue
            if not _traffic_compatible(session, gate):
                continue
            result.append(gate)
        return result

    decision_ids = [sid for sid in changed if sid in by_id]
    terminal_candidate_map = {
        sid: candidates_for(by_id[sid], include_remote=False)
        for sid in decision_ids
    }
    remote_candidate_map = {
        sid: [
            gate for gate in candidates_for(by_id[sid], include_remote=True)
            if gate.get("kind") == "remote"
        ]
        for sid in decision_ids
    }

    decision_ids.sort(key=lambda sid: (
        # Aircraft already on a gate claim first; arrivals plan around them.
        0 if by_id[sid].get("initiallyParked", False) or "physicalGate" in by_id[sid] else 1,
        0 if by_id[sid].get("priority", False) else 1,
        len(terminal_candidate_map[sid]),
        0 if sid in previous else 1,
        sid,
    ))
    terminal_candidate_map, remote_candidate_map = {}, {}

    def ordered(ids):
        for sid in ids:
            terminal_candidate_map[sid] = candidates_for(by_id[sid], include_remote=False)
            remote_candidate_map[sid] = [gate for gate in candidates_for(by_id[sid], include_remote=True)
                                         if gate.get("kind") == "remote"]
        return sorted(ids, key=lambda sid: (
            0 if by_id[sid].get("initiallyParked", False) else 1,
            0 if by_id[sid].get("priority", False) else 1,
            len(terminal_candidate_map[sid]),
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

    def place(sid):
        session = by_id[sid]
        old_gate = previous.get(sid)
        start, end = float(session["start"]), float(session["end"])
        initially_parked = bool(session.get("initiallyParked", False)) or sid in mandatory

        def ranked_cost(gate: dict[str, Any], *, preserve_old: bool = True) -> float:
            index = gate_index[str(gate["id"])]
            value = _gate_assignment_cost(session, gate, index)
            if preserve_old and session.get("occupiedGate", old_gate) == str(gate["id"]):
                value -= 1_000_000.0
            return value

        selected: str | None = None
        selected_start: float | None = None

        physical_gate = session.get("physicalGate")
        if physical_gate is not None and physical_gate in gate_by_id:
            # Already on a gate: it holds that gate until towed, and a tow can only
            # go somewhere free now. Staying put saves the tow.
            now = float(session["physicalSince"])
            from_now = {**session, "start": now}
            options = []
            for gate in terminal_candidate_map[sid] + remote_candidate_map[sid]:
                available = _earliest_gate_start(from_now, str(gate["conflictGroup"]), occupied, outages)
                if available is not None and available <= now:
                    cost = ranked_cost(gate, preserve_old=False)
                    if str(gate["id"]) != physical_gate:
                        cost += TOW_PENALTY
                    options.append((cost, str(gate["id"])))
            # With nowhere to go (e.g. its own gate just closed), it physically stays.
            selected = min(options)[1] if options else physical_gate
            selected_start = start if selected == physical_gate else now
        # Initially parked aircraft already occupy the apron. They cannot
        # remain on the taxiway/tarmac waiting for a later slot.
        elif initially_parked:
            immediate_options = []
            for gate in terminal_candidate_map[sid] + remote_candidate_map[sid]:
                available = _earliest_gate_start(
                    session, str(gate["conflictGroup"]), occupied, outages
                )
                if available is not None and available <= start:
                    immediate_options.append((ranked_cost(gate), str(gate["id"])))
            if immediate_options:
                _, selected = min(immediate_options)
                selected_start = start
        else:
            # First consider all compatible terminal gates that become
            # available within the configured tarmac-wait window.
            terminal_options = []
            latest_terminal_start = min(start + MAX_TARMAC_WAIT_MINUTES, end)
            for gate in terminal_candidate_map[sid]:
                available = _earliest_gate_start(
                    session, str(gate["conflictGroup"]), occupied, outages
                )
                if available is None or available > latest_terminal_start or available >= end:
                    continue
                wait_minutes = max(0.0, available - start)
                score = (
                    wait_minutes * WAITING_PENALTY_PER_MINUTE
                    + ranked_cost(gate)
                )
                terminal_options.append((score, available, str(gate["id"])))

            if terminal_options:
                _, selected_start, selected = min(terminal_options)

            # A medically authorized emergency/diversion may use remote
            # overflow immediately rather than incur a prolonged tarmac hold.
            # Ordinary flights use remote overflow only after the full wait
            # window has elapsed without a compatible terminal slot.
            if not terminal_options or session.get("remoteAllowed", False):
                remote_options = []
                for gate in remote_candidate_map[sid]:
                    available = _earliest_gate_start(
                        session, str(gate["conflictGroup"]), occupied, outages
                    )
                    if available is None:
                        continue

                    if session.get("remoteAllowed", False):
                        remote_start = available
                        if remote_start > start + MAX_TARMAC_WAIT_MINUTES or remote_start >= end:
                            continue
                    else:
                        remote_start = start + MAX_TARMAC_WAIT_MINUTES
                        if remote_start >= end or available > remote_start:
                            continue

                    score = (
                        max(0.0, remote_start - start) * WAITING_PENALTY_PER_MINUTE
                        + ranked_cost(gate, preserve_old=True)
                    )
                    remote_options.append((score, remote_start, str(gate["id"])))

                if remote_options:
                    remote_score, remote_start, remote_gate = min(remote_options)
                    terminal_score = min((item[0] for item in terminal_options), default=float("inf"))
                    if not terminal_options or remote_score < terminal_score:
                        selected = remote_gate
                        selected_start = remote_start

        if selected is None or selected_start is None:
            if sid in mandatory:
                raise RuntimeError(f"No immediate compatible gate for occupied aircraft {session['registration']!r}.")
            unassigned.append(sid)
            all_candidates = terminal_candidate_map[sid] + remote_candidate_map[sid]
            if not all_candidates:
                unassigned_reasons[sid] = "no_traffic_compatible_gate_or_width_match"
            elif initially_parked:
                unassigned_reasons[sid] = "initially_parked_no_gate_available_at_start"
            else:
                unassigned_reasons[sid] = "no_gate_slot_within_tarmac_wait_or_overflow_window"
            return

        selected_group = str(gate_by_id[selected]["conflictGroup"])
        occupied[selected_group].append((float(selected_start), end))
        gate_starts[sid] = float(selected_start)

        if selected_start > start:
            waiting[sid] = {
                "gate": selected,
                "arrival_minute": start,
                "gate_entry_minute": float(selected_start),
                "wait_minutes": float(selected_start) - start,
                "overflow": gate_by_id[selected].get("kind") == "remote",
            }

        if old_gate is None:
            new_assignments.append([sid, selected])
        elif selected != old_gate:
            reassignments.append([sid, selected])
        else:
            retained_assignments.append([sid, selected])

    # Reserve physical aircraft before optional bookings; validate the complete
    # batch before publishing it so failed relocations never release a source gate.
    for sid in ordered(mandatory):
        place(sid)

    for sid, gate_id in previous.items():
        if sid in mandatory or sid in changed or sid not in by_id or gate_id not in gate_by_id:
            continue
        session = by_id[sid]
        booking_start = float(previous_gate_starts.get(sid, session["start"]))
        end = float(session["end"])
        group = str(gate_by_id[gate_id]["conflictGroup"])
        if any(booking_start < b and a < end for a, b in occupied[group]):
            changed.add(sid)
        else:
            occupied[group].append((booking_start, end))

    for sid in ordered({sid for sid in changed - mandatory if sid in by_id}):
        place(sid)

    return {
        "assignments": new_assignments,
        "reassignments": reassignments,
        "retained_assignments": retained_assignments,
        "unassigned": unassigned,
        "unassigned_reasons": unassigned_reasons,
        "gate_starts": gate_starts,
        "waiting": waiting,
        "repaired_sessions": sorted((changed | mandatory) & by_id.keys()),
    }


def validate_assignments(sessions, gates, outages, assignments, gate_starts):
    """Reject an incomplete physical reservation or an overlapping complete plan."""
    if assignments.keys() - sessions.keys():
        raise RuntimeError("Gate plan contains an unknown parking session.")
    gate_by_id = {str(gate["id"]): gate for gate in gates}
    bookings = defaultdict(list)
    for sid, session in sessions.items():
        gate_id = assignments.get(sid)
        mandatory = session.get("occupiedGate") or session.get("fixedGate")
        start = float(gate_starts.get(sid, session["start"]))
        if mandatory and (gate_id is None or start > float(session["start"])):
            raise RuntimeError(f"Missing immediate reservation for occupied aircraft {session['registration']!r}.")
        if gate_id is None:
            continue
        gate = gate_by_id.get(gate_id)
        if (gate is None or not allowed_width(session.get("widthClass"), gate.get("widthClass"))
                or not _traffic_compatible(session, gate)):
            raise RuntimeError(f"Invalid passenger gate {gate_id!r} for session {sid!r}.")
        if session.get("fixedGate") and gate_id != session["fixedGate"]:
            raise RuntimeError(f"Initial occupied gate changed for session {sid!r}.")
        end = float(session["end"])
        if start < float(session["start"]) or start >= end:
            raise RuntimeError(f"Invalid gate reservation interval for session {sid!r}.")
        group = str(gate["conflictGroup"])
        if any(start < b and a < end for a, b in outages.get(group, [])):
            raise RuntimeError(f"Passenger gate reservation overlaps outage for session {sid!r}.")
        bookings[group].append((start, end, sid))
    for group, intervals in bookings.items():
        intervals.sort()
        if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
            raise RuntimeError(f"Overlapping passenger gate reservations in conflict group {group!r}.")


def build_solver_payload(
    sessions: dict[str, dict[str, Any]],
    gates: list[dict[str, str]],
    outages: dict[str, list[list[float]]],
) -> dict[str, Any]:
    return {
        "sessions": list(sessions.values()),
        "gates": gates,
        "outages": outages,
        "costs": {
            "walkingDistancePerClass": WALKING_DISTANCE_PENALTY_PER_CLASS,
            "domesticToInternationalPenalty": DOMESTIC_TO_INTERNATIONAL_PENALTY,
            "emergencyRemotePenalty": EMERGENCY_GATE_PENALTY,
        },
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
    handler.send_header("Cache-Control", "no-store")
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
    gate_kinds = {
        str(gate.get("id")): gate.get("kind")
        for gate in (STATE.gates if STATE is not None else [])
    }
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
                is_overflow = gate_kinds.get(str(assignments[sid])) == "remote"
                waiting_sessions[sid] = {
                    "gate": assignments[sid],
                    "arrival_minute": start,
                    "gate_entry_minute": gate_start,
                    "wait_minutes": gate_start - start,
                    "overflow": is_overflow,
                }
                waiting_aircraft[sid] = {
                    **record,
                    "status": (
                        "waiting_for_overflow_stand"
                        if is_overflow else "waiting_for_reserved_gate"
                    ),
                    "wait_minutes": gate_start - start,
                    "overflow": is_overflow,
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
PREDICTED_GATE_STATE: dict[str, Any] = {}
PREDICTED_UNASSIGNED_AIRCRAFT: list[dict[str, Any]] = []
LATEST_UNASSIGNED_AIRCRAFT: list[dict[str, Any]] = []
LATEST_SIMULATED_TIME: str | None = None
LAST_SOLVER_SUMMARY: dict[str, Any] = {}
LAST_DECISION_RESPONSE: dict[str, Any] = {"assignments": [], "moves": []}
# Dashboard history, replaced (never mutated) on each commit.
ASSIGNED_AT: dict[str, str] = {}  # session -> simulated UTC time its current gate was chosen
VISIT_POSITIONS: dict[str, tuple[tuple[float, str, str | None], ...]] = {}  # session -> (minute, gate, assignedAt)
UNASSIGNED_REASONS: dict[str, str] = {}
DEFERRED_SESSIONS: dict[str, str] = {}  # current-visit tows held back because the stream would refuse them
MISSED_EVENTS: list[list[int]] = []  # inclusive event index ranges the stream skipped

# Every global a webhook may assign; restored together if the event fails.
_EVENT_GLOBALS = (
    "STATE", "ASSIGNMENTS", "GATE_STARTS", "CURRENT_RUN_ID", "LAST_EVENT_INDEX", "LAST_EVENT_ID",
    "LATEST_GATE_STATE", "PREDICTED_GATE_STATE", "PREDICTED_UNASSIGNED_AIRCRAFT",
    "LATEST_UNASSIGNED_AIRCRAFT", "LATEST_SIMULATED_TIME", "LAST_SOLVER_SUMMARY",
    "LAST_DECISION_RESPONSE", "ASSIGNED_AT", "VISIT_POSITIONS", "UNASSIGNED_REASONS",
    "DEFERRED_SESSIONS", "MISSED_EVENTS",
)

# Idempotency: X-Stream-Event-Id -> the committed response. A retried delivery gets
# the same answer without applying the event twice.
RESPONSE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
RESPONSE_CACHE_SIZE = 32

VIEW_CLOCK = SimClock()


def active_token_legs(state: ScenarioState) -> dict[str, dict[str, Any]]:
    active = {}
    for route in state.routes.values():
        for entry in route["legs"]:
            if entry["active"]:
                active[entry["token"]] = state.token_legs.get(entry["token"], {
                    "carrier": route["carrier"], "leg": entry["leg"]
                })
    return active


def current_minute(state: ScenarioState, simulated_time: str | None = None) -> float:
    simulated_time = simulated_time or LATEST_SIMULATED_TIME
    if not simulated_time:
        return 0.0
    local_now = local_event_time(simulated_time, state.airport_tz)
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
    snapshot: dict[str, Any] | None = None,
    simulated_time: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Translate internal parking-session choices into the stream's exact contract."""
    selected = set(sessions) if include_all else set(session_ids or ())
    active_refs = active_token_legs(state)
    decision_map: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
    conflicting: set[tuple[Any, ...]] = set()
    move_map: dict[str, dict[str, str]] = {}
    snapshot = snapshot if snapshot is not None else {
        "gateState": LATEST_GATE_STATE, "unassignedAircraft": LATEST_UNASSIGNED_AIRCRAFT,
    }
    occupied_now = {
        str(plane.get("registration")): gate
        for gate, plane in snapshot.get("gateState", {}).items() if plane is not None
    }
    unassigned_now = {
        str(plane.get("registration"))
        for plane in snapshot.get("unassignedAircraft", []) if plane.get("registration")
    }
    now_minute = current_minute(state, simulated_time)

    for sid in selected:
        session = sessions.get(sid)
        gate = assignments.get(sid)
        if session is None:
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
            gate = None

        # A merged visit (cancelled departure) has several arrivals; only end_token departs.
        for token in session.get("leg_tokens", (session.get("start_token"), session.get("end_token"))):
            role = 1 if token == session.get("end_token") else 0
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
            # A YYZ->YYZ leg ends one visit and starts the next. The stream places an
            # arriving aircraft by that leg's assignment, so the arrival role wins.
            previous = decision_map.get(identity)
            if previous is not None and previous[1]["gate"] != gate:
                if previous[0] == role:
                    print(f"DECISION SKIPPED: leg {identity} has two {('arrival', 'departure')[role]} "
                          f"gates ({previous[1]['gate']}, {gate})", file=sys.stderr, flush=True)
                    conflicting.add(identity)
                    continue
                if previous[0] < role:
                    continue
            decision_map[identity] = (role, decision)

        # A currently grounded aircraft may need an immediate tow. Emit the
        # explicit move as well as leg assignments so sessions without a
        # matching active leg (e.g. idle parked aircraft) are still actionable.
        if is_current and gate is not None:
            current_gate = occupied_now.get(registration)
            if current_gate != gate:
                move_map[registration] = {"registration": registration, "gate": gate}

    return {
        "assignments": [
            decision for identity, (_, decision) in decision_map.items()
            if identity not in conflicting
        ],
        "moves": list(move_map.values()),
    }


def defer_blocked_current_visits(
    state: ScenarioState,
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_starts: dict[str, float],
    session_ids: set[str],
) -> tuple[set[str], dict[str, str]]:
    """Hold back current-visit tows the stream would refuse right now.

    The plan can disagree with physical occupancy (late departures, refused
    arrivals). The stream rejects a tow into an occupied, overlapping, or closed
    position, so those sessions are left out of this response and returned with a
    reason; the next event repairs them again. Everything else is still sent.
    """
    occupied_now = {
        str(plane.get("registration")): gate
        for gate, plane in LATEST_GATE_STATE.items() if plane is not None
    }
    grounded = set(occupied_now) | {
        str(plane.get("registration"))
        for plane in LATEST_UNASSIGNED_AIRCRAFT if plane.get("registration")
    }
    now_minute = current_minute(state)
    outages = state.outage_payload()
    targets: dict[str, tuple[str, str]] = {}
    for sid in session_ids:
        session, gate = sessions.get(sid), assignments.get(sid)
        if session is None or gate is None:
            continue
        registration = str(session.get("registration", ""))
        if registration not in grounded:
            continue
        if not (float(session["start"]) <= now_minute < float(session["end"])):
            continue
        if float(gate_starts.get(sid, session["start"])) > now_minute:
            continue
        targets[sid] = (registration, gate)

    deferred: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        # Deferred aircraft stay where they are and may now block someone else.
        movers = {
            registration for sid, (registration, gate) in targets.items()
            if sid not in deferred and occupied_now.get(registration) != gate
        }
        holders: dict[str, str] = {}
        for gate_id, plane in LATEST_GATE_STATE.items():
            record = state.gate_by_id.get(gate_id)
            if plane is not None and record and str(plane.get("registration")) not in movers:
                holders[str(record["conflictGroup"])] = str(plane.get("registration"))
        claimed: dict[str, str] = {}
        for sid, (registration, gate) in sorted(targets.items()):
            if sid in deferred:
                continue
            record = state.gate_by_id.get(gate)
            reason = None
            if record is None:
                reason = f"unknown gate {gate!r}"
            else:
                group = str(record["conflictGroup"])
                holder = holders.get(group)
                if holder is not None and holder != registration:
                    reason = f"{gate} is physically occupied by {holder}"
                elif claimed.get(group, registration) != registration:
                    reason = f"{gate} is also the target of {claimed[group]}"
                elif occupied_now.get(registration) != gate and any(
                    float(start) <= now_minute < float(end) for start, end in outages.get(group, [])
                ):
                    reason = f"{gate} is out of service"
                else:
                    claimed[group] = registration
            if reason is not None:
                deferred[sid] = reason
                changed = True
    for sid, reason in sorted(deferred.items()):
        print(f"TOW DEFERRED: {sid} ({targets[sid][0]}): {reason}", file=sys.stderr, flush=True)
    return {sid for sid in session_ids if sid not in deferred}, deferred


def stranded_current_visits(
    state: ScenarioState,
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_starts: dict[str, float],
) -> set[str]:
    """Current visits whose aircraft is not at its planned gate although it could be.

    Covers decisions the stream refused or never received; resending them lets
    the plan converge on physical occupancy without a full resync.
    """
    position = {
        str(plane.get("registration")): gate
        for gate, plane in LATEST_GATE_STATE.items() if plane is not None
    }
    grounded = set(position) | {
        str(plane.get("registration"))
        for plane in LATEST_UNASSIGNED_AIRCRAFT if plane.get("registration")
    }
    now_minute = current_minute(state)
    return {
        sid for sid, session in sessions.items()
        if sid in assignments
        and str(session.get("registration", "")) in grounded
        and float(session["start"]) <= now_minute < float(session["end"])
        and float(gate_starts.get(sid, session["start"])) <= now_minute
        and position.get(str(session.get("registration", ""))) != assignments[sid]
    }


def predict_post_decision_gate_state(
    state: ScenarioState,
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_starts: dict[str, float],
    session_ids: set[str],
    *,
    snapshot: dict[str, Any] | None = None,
    simulated_time: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mirror only current-visit moves; future assignments do not occupy gates."""
    snapshot = snapshot if snapshot is not None else {
        "gateState": LATEST_GATE_STATE, "unassignedAircraft": LATEST_UNASSIGNED_AIRCRAFT,
    }
    predicted = copy.deepcopy(snapshot.get("gateState", {}))
    unassigned = copy.deepcopy(snapshot.get("unassignedAircraft", []))
    occupied_now = {
        str(plane.get("registration")): gate
        for gate, plane in predicted.items() if plane is not None
    }
    unassigned_now = {
        str(plane.get("registration"))
        for plane in unassigned if plane.get("registration")
    }
    now_minute = current_minute(state, simulated_time)
    targets: dict[str, tuple[str, dict[str, Any]]] = {}

    for sid in session_ids:
        session = sessions.get(sid)
        gate = assignments.get(sid)
        if session is None or gate is None:
            continue
        registration = str(session.get("registration", ""))
        if registration not in occupied_now and registration not in unassigned_now:
            continue
        if not (float(session["start"]) <= now_minute < float(session["end"])):
            continue
        if float(gate_starts.get(sid, session["start"])) > now_minute:
            continue
        plane = {
            "carrier": session.get("carrier"),
            "registration": registration,
            "aircraftType": session.get("aircraftType"),
        }
        previous = targets.get(registration)
        if previous is not None and previous[0] != gate:
            print(f"PREDICTION SKIPPED: {registration} has two current gate targets", file=sys.stderr, flush=True)
            continue
        targets[registration] = (gate, plane)

    target_registrations = set(targets)
    for gate_id, occupant in list(predicted.items()):
        if occupant is not None and str(occupant.get("registration")) in target_registrations:
            predicted[gate_id] = None

    group_occupants: dict[str, str] = {}
    for gate_id, occupant in predicted.items():
        if occupant is None:
            continue
        gate_record = state.gate_by_id.get(gate_id)
        if gate_record:
            group_occupants[str(gate_record["conflictGroup"])] = str(occupant.get("registration"))

    for registration, (gate_id, plane) in targets.items():
        gate_record = state.gate_by_id.get(gate_id)
        group = str(gate_record["conflictGroup"]) if gate_record else None
        blocker = group_occupants.get(group)
        if gate_record is None or (blocker is not None and blocker != registration):
            # defer_blocked_current_visits keeps these out of responses; never fail the event.
            print(f"PREDICTION SKIPPED: {registration} cannot enter {gate_id!r}", file=sys.stderr, flush=True)
            continue
        group_occupants[group] = registration
        predicted[gate_id] = plane

    unassigned = [
        plane for plane in unassigned
        if str(plane.get("registration")) not in target_registrations
    ]
    return predicted, unassigned


def dashboard_snapshot() -> dict[str, Any]:
    if STATE is None:
        return {
            "status": "waiting_for_initialization",
            "run_id": CURRENT_RUN_ID,
            "last_event_id": LAST_EVENT_ID,
            "simulated_time": LATEST_SIMULATED_TIME,
            "airport_timezone": None,
            "schedule_epoch": None,
            "current_minute": 0,
            "assignments": [],
            "gate_assignments": [],
            "gate_state": {},
            "gate_state_source": None,
            "unassigned_aircraft": [],
            "gates": [],
            "last_decision_response": copy.deepcopy(LAST_DECISION_RESPONSE),
            "last_decision_timestamp": None,
        }

    sessions = STATE.build_sessions()
    status = build_aircraft_status(sessions, ASSIGNMENTS, GATE_STARTS)
    full = build_stream_decisions(
        STATE, sessions, ASSIGNMENTS, GATE_STARTS, include_all=True,
        respect_gate_start=False
    )
    gate_state_for_dashboard = PREDICTED_GATE_STATE or LATEST_GATE_STATE
    unassigned_for_dashboard = (
        PREDICTED_UNASSIGNED_AIRCRAFT
        if PREDICTED_GATE_STATE else LATEST_UNASSIGNED_AIRCRAFT
    )
    occupants = {
        gate: plane for gate, plane in gate_state_for_dashboard.items() if plane is not None
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
            "waiting_minutes": max(
                0.0,
                float(GATE_STARTS.get(sid, session.get("start"))) - float(session.get("start"))
            ),
            "gate_kind": (
                STATE.gate_by_id.get(gate, {}).get("kind") if gate else None
            ),
            "overflow": bool(
                gate and STATE.gate_by_id.get(gate, {}).get("kind") == "remote"
            ),
            "status": (
                "unassigned" if gate is None else
                "waiting_for_overflow" if (
                    STATE.gate_by_id.get(gate, {}).get("kind") == "remote"
                    and GATE_STARTS.get(sid, session.get("start")) > session.get("start")
                ) else
                "waiting_for_gate" if GATE_STARTS.get(sid, session.get("start")) > session.get("start")
                else "assigned"
            ),
            "traffic_types": session.get("trafficTypes", []),
            "arrival_traffic_type": session.get("arrivalTrafficType"),
            "departure_traffic_type": session.get("departureTrafficType"),
            "walking_distance_class": (
                STATE.gate_by_id.get(gate, {}).get("walkingDistanceClass") if gate else None
            ),
            "preferred_terminal": session.get("preferredTerminal"),
        })

    return {
        "status": "ready",
        "run_id": CURRENT_RUN_ID,
        "last_event_id": LAST_EVENT_ID,
        "simulated_time": LATEST_SIMULATED_TIME,
        "airport_timezone": STATE.airport_tz.key,
        "schedule_epoch": STATE.epoch.replace(tzinfo=STATE.airport_tz).isoformat(),
        "current_minute": current_minute(STATE),
        "assignments": full["assignments"],
        "gate_assignments": gate_assignments,
        "unassigned": status["unassigned_sessions"],
        "unassigned_details": status["unassigned_details"],
        "waiting_sessions": status["waiting_sessions"],
        "waiting_aircraft": status["waiting_aircraft"],
        "gate_state": copy.deepcopy(gate_state_for_dashboard),
        "gate_state_before_decision": copy.deepcopy(LATEST_GATE_STATE),
        "gate_state_source": "matcher-predicted post-decision; stream event feed is authoritative",
        "unassigned_aircraft": copy.deepcopy(unassigned_for_dashboard),
        "gates": gates,
        "outages": copy.deepcopy(STATE.outages),
        "solver": copy.deepcopy(LAST_SOLVER_SUMMARY),
        "last_decision_response": copy.deepcopy(LAST_DECISION_RESPONSE),
        "last_decision_timestamp": LATEST_SIMULATED_TIME,
    }


def track_assignment_history(
    state: ScenarioState,
    sessions: dict[str, dict[str, Any]],
    assignments: dict[str, str],
    gate_state: dict[str, Any],
    simulated_time: str,
    previous_assignments: dict[str, str],
    previous_assigned_at: dict[str, str],
    previous_positions: dict[str, tuple[tuple[float, str, str | None], ...]],
) -> tuple[dict[str, str], dict[str, tuple[tuple[float, str, str | None], ...]]]:
    """Return when each current gate was chosen, and the gates each visit physically held.

    Positions come from the post-decision gate state, so a tow at this event
    appears as a new segment starting now.
    """
    assigned_at = {
        sid: (previous_assigned_at.get(sid) or simulated_time)
        if previous_assignments.get(sid) == gate else simulated_time
        for sid, gate in assignments.items()
    }
    position = {
        str(plane.get("registration")): gate
        for gate, plane in gate_state.items() if plane is not None
    }
    now_minute = current_minute(state)
    positions = {}
    for sid, session in sessions.items():
        segments = previous_positions.get(sid, ())
        gate = position.get(str(session.get("registration", "")))
        if (gate is not None and float(session["start"]) <= now_minute < float(session["end"])
                and (not segments or segments[-1][1] != gate)):
            chosen = assigned_at.get(sid) if assignments.get(sid) == gate else simulated_time
            segments = (*segments, (now_minute, gate, chosen))
        if segments:
            positions[sid] = segments
    return assigned_at, positions


def _empty_repair_result() -> dict[str, Any]:
    return {
        "assignments": [], "reassignments": [], "retained_assignments": [],
        "unassigned": [], "unassigned_reasons": {}, "gate_starts": {}, "waiting": {},
    }


def _handle_initialize(message: dict[str, Any], simulated_time: str) -> dict[str, Any]:
    global STATE, ASSIGNMENTS, GATE_STARTS
    global LATEST_GATE_STATE, PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT
    global LATEST_UNASSIGNED_AIRCRAFT, LATEST_SIMULATED_TIME, LAST_SOLVER_SUMMARY
    global LAST_DECISION_RESPONSE, ASSIGNED_AT, VISIT_POSITIONS, UNASSIGNED_REASONS
    global DEFERRED_SESSIONS

    aircraft_classes, gates, airport_timezone = load_references()
    candidate = ScenarioState({}, aircraft_classes, gates, airport_timezone)
    candidate.initialize({"timestamp": simulated_time, "message": message})
    sessions = candidate.build_sessions(gate_state=message.get("gateState", {}), simulated_time=simulated_time)

    # The stream already has physical positions for parked aircraft. Lock
    # those positions for the initial solve; never silently reshuffle the
    # initialization snapshot.
    for session in sessions.values():
        if session.get("occupiedGate"):
            session["fixedGate"] = session["occupiedGate"]

    payload = build_solver_payload(sessions, gates, candidate.outage_payload())
    payload["mode"] = "initial"
    payload["timeLimit"] = GUROBI_TIME_LIMIT
    payload["threads"] = GUROBI_THREADS
    # No greedy fallback: if Julia fails, initialization fails, nothing is committed,
    # and the stream retries it (by then Julia has restarted).
    result = run_julia(SOLVER_BACKEND, payload)
    assignments = {
        sid: gate for sid, gate in assignment_map(result.get("assignments", [])).items()
        if sid in sessions and gate in candidate.gate_by_id
    }
    gate_starts = {sid: float(sessions[sid]["start"]) for sid in assignments}

    # Keep the existing local repair fallback for sessions the joint model
    # cannot place. Conflict-group occupancy and crosswalk capacity are used
    # by the repair path too.
    initial_unassigned = [sid for sid in sessions if sid not in assignments]
    waiting_result = repair_assignments_python(
        sessions, gates, candidate.outage_payload(), assignments,
        set(initial_unassigned), gate_starts,
    ) if initial_unassigned else _empty_repair_result()
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

    decision_ids, deferred = defer_blocked_current_visits(
        candidate, sessions, assignments, gate_starts, set(sessions)
    )
    decisions = build_stream_decisions(candidate, sessions, assignments, gate_starts, decision_ids)
    PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT = predict_post_decision_gate_state(
        candidate, sessions, assignments, gate_starts, decision_ids
    )
    ASSIGNED_AT, VISIT_POSITIONS = track_assignment_history(
        candidate, sessions, assignments, PREDICTED_GATE_STATE, simulated_time, {}, {}, {}
    )
    UNASSIGNED_REASONS = {
        sid: reason for sid, reason in waiting_result.get("unassigned_reasons", {}).items()
        if sid not in assignments
    }
    DEFERRED_SESSIONS = deferred
    LAST_SOLVER_SUMMARY = {
    ) if initial_unassigned else {
        "assignments": [], "reassignments": [], "retained_assignments": [],
        "unassigned": [], "unassigned_reasons": {}, "gate_starts": {}, "waiting": {},
    }
    for sid in waiting_result.get("repaired_sessions", []):
        assignments.pop(sid, None)
        gate_starts.pop(sid, None)
    for key in ("assignments", "reassignments", "retained_assignments"):
        assignments.update(assignment_map(waiting_result.get(key, [])))
    gate_starts.update(waiting_result.get("gate_starts", {}))

    validate_assignments(sessions, gates, candidate.outage_payload(), assignments, gate_starts)
    decisions = build_stream_decisions(
        candidate, sessions, assignments, gate_starts, include_all=True,
        snapshot=message, simulated_time=simulated_time,
    )
    predicted, unassigned = predict_post_decision_gate_state(
        candidate, sessions, assignments, gate_starts, set(sessions),
        snapshot=message, simulated_time=simulated_time,
    )
    solver_summary = {
        "mode": "initial",
        "solver": "julia",
        "termination_status": result.get("termination_status"),
        "objective": result.get("objective"),
        "sessions": len(sessions),
        "assigned_sessions": len(assignments),
        "unassigned_sessions": sorted(sid for sid in sessions if sid not in assignments),
        "waiting_sessions": waiting_result.get("waiting", {}),
        "waiting_count": len(waiting_result.get("waiting", {})),
        "deferred_tows": deferred,
    }

    STATE, ASSIGNMENTS, GATE_STARTS = candidate, assignments, gate_starts
    LATEST_GATE_STATE = copy.deepcopy(message.get("gateState", {}))
    LATEST_UNASSIGNED_AIRCRAFT = copy.deepcopy(message.get("unassignedAircraft", []))
    LATEST_SIMULATED_TIME = simulated_time
    PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT = predicted, unassigned
    LAST_SOLVER_SUMMARY = solver_summary
    LAST_DECISION_RESPONSE = copy.deepcopy(decisions)
    PASSENGER_LAST_FIELDS = {}
    PASSENGER_PREVIOUS_FIELDS = {}
    LAST_PASSENGER_FLIGHTS_JSON = None
    return decisions


def _handle_update(message: dict[str, Any], simulated_time: str, *, resync: bool = False) -> dict[str, Any]:
    global STATE, ASSIGNMENTS, GATE_STARTS
    global LATEST_GATE_STATE, PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT
    global LATEST_UNASSIGNED_AIRCRAFT, LATEST_SIMULATED_TIME, LAST_SOLVER_SUMMARY
    global LAST_DECISION_RESPONSE, ASSIGNED_AT, VISIT_POSITIONS, UNASSIGNED_REASONS
    global DEFERRED_SESSIONS

    if STATE is None or not STATE.initialized:
        raise RuntimeError("Matcher has not received InitializeAirport.")

    previous_assignments = ASSIGNMENTS
    candidate = copy.deepcopy(STATE)
    before = candidate.build_sessions(gate_state=PREDICTED_GATE_STATE or LATEST_GATE_STATE,
                                      simulated_time=LATEST_SIMULATED_TIME)
    action = candidate.apply_message({"timestamp": simulated_time, "message": message})
    candidate.validate_timezone_coverage()
    _refresh_horizon(candidate)
    after = candidate.build_sessions(gate_state=message.get("gateState", {}), simulated_time=simulated_time)
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

    # Aircraft physically on a gate keep it until towed. Re-plan any that diverge
    # from the plan, and any future booking that overlaps an incumbent's stay.
    now_minute = minutes_from_epoch(local_event_time(simulated_time, candidate.airport_tz), candidate.epoch)
    physical = {
        str(plane.get("registration")): gate
        for gate, plane in (message.get("gateState") or {}).items()
        if plane is not None and gate in candidate.gate_by_id
    }
    grounded_ids = set()
    for sid, session in after.items():
        gate = physical.get(str(session.get("registration", "")))
        if gate is not None and float(session["start"]) <= now_minute < float(session["end"]):
            session["physicalGate"], session["physicalSince"] = gate, now_minute
            grounded_ids.add(sid)
    diverged_ids = {sid for sid in grounded_ids if assignments.get(sid) != after[sid]["physicalGate"]}
    incumbent_blocks = defaultdict(list)
    for sid in grounded_ids:
        group = candidate.gate_by_id[after[sid]["physicalGate"]]["conflictGroup"]
        incumbent_blocks[group].append((sid, now_minute, float(after[sid]["end"])))
    bumped_ids = set()
    for sid, gate in assignments.items():
        if sid in grounded_ids or sid not in after or gate not in candidate.gate_by_id:
            continue
        booking_start = float(gate_starts.get(sid, after[sid]["start"]))
        for other, block_start, block_end in incumbent_blocks.get(candidate.gate_by_id[gate]["conflictGroup"], []):
            if other != sid and booking_start < block_end and block_start < float(after[sid]["end"]):
                bumped_ids.add(sid)
                break

    waiting_ids = {
        sid for sid, gate in assignments.items()
        if sid in after
        and float(gate_starts.get(sid, after[sid]["start"])) > float(after[sid]["start"])
        and sid not in grounded_ids
    }
    unassigned_ids = {sid for sid in after if sid not in assignments}
    # Tows the stream could not take last time are planned again.
    deferred_ids = set(DEFERRED_SESSIONS) & set(after)
    repair_ids = set(changed) | waiting_ids | unassigned_ids | deferred_ids | diverged_ids | bumped_ids

    solver_result: dict[str, Any] = _empty_repair_result()
    if repair_ids:
        solver_result = repair_assignments_python(
            after, candidate.gates, outages, assignments, repair_ids, gate_starts
        )
        repair_ids.update(solver_result.get("repaired_sessions", []))
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

    STATE = candidate
    ASSIGNMENTS = assignments
    GATE_STARTS = gate_starts
    LATEST_GATE_STATE = copy.deepcopy(message.get("gateState", {}))
    LATEST_UNASSIGNED_AIRCRAFT = copy.deepcopy(message.get("unassignedAircraft", []))
    LATEST_SIMULATED_TIME = simulated_time

    decision_sessions = set()
    for key in ("assignments", "reassignments", "retained_assignments"):
        decision_sessions.update(str(pair[0]) for pair in solver_result.get(key, []))
    # Resend current visits the stream never applied (refused, or lost with a skipped event).
    stranded = stranded_current_visits(candidate, after, assignments, gate_starts)
    decision_sessions |= stranded
    if resync:
        # The stream missed at least one response; send the whole plan again.
        decision_sessions = set(after)
    decision_ids, deferred = defer_blocked_current_visits(
        candidate, after, assignments, gate_starts, decision_sessions
    )
    decisions = build_stream_decisions(candidate, after, assignments, gate_starts, decision_ids)
    PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT = predict_post_decision_gate_state(
        candidate, after, assignments, gate_starts, decision_ids
    )
    ASSIGNED_AT, VISIT_POSITIONS = track_assignment_history(
        candidate, after, assignments, PREDICTED_GATE_STATE, simulated_time,
        previous_assignments, ASSIGNED_AT, VISIT_POSITIONS,
    )
    UNASSIGNED_REASONS = {
        sid: reason for sid, reason in {
            **{sid: reason for sid, reason in UNASSIGNED_REASONS.items() if sid not in repair_ids},
            **solver_result.get("unassigned_reasons", {}),
        }.items()
        if sid in after and sid not in assignments
    }
    DEFERRED_SESSIONS = deferred
    LAST_SOLVER_SUMMARY = {
    validate_assignments(after, candidate.gates, outages, assignments, gate_starts)
    decisions = build_stream_decisions(
        candidate, after, assignments, gate_starts, repair_ids,
        snapshot=message, simulated_time=simulated_time,
    )
    predicted, unassigned = predict_post_decision_gate_state(
        candidate, after, assignments, gate_starts, repair_ids,
        snapshot=message, simulated_time=simulated_time,
    )
    solver_summary = {
        "mode": "repair",
        "action": action,
        "resync": resync,
        "changed_sessions": sorted(changed),
        "reconsidered_waiting_sessions": sorted(waiting_ids),
        "reconsidered_deferred_sessions": sorted(deferred_ids),
        "replanned_diverged_grounded_sessions": sorted(diverged_ids),
        "replanned_bookings_overlapping_incumbents": sorted(bumped_ids),
        "resent_stranded_sessions": sorted(stranded),
        "removed_sessions": removed,
        "sessions": len(after),
        "assigned_sessions": len(assignments),
        "unassigned_sessions": sorted(sid for sid in after if sid not in assignments),
        "new_assignments": len(solver_result.get("assignments", [])),
        "reassignments": len(solver_result.get("reassignments", [])),
        "waiting_sessions": solver_result.get("waiting", {}),
        "waiting_count": len(solver_result.get("waiting", {})),
        "deferred_tows": deferred,
    }
    # Publish only after the complete reservation plan and atomic move batch pass.
    STATE, ASSIGNMENTS, GATE_STARTS = candidate, assignments, gate_starts
    LATEST_GATE_STATE = copy.deepcopy(message.get("gateState", {}))
    LATEST_UNASSIGNED_AIRCRAFT = copy.deepcopy(message.get("unassignedAircraft", []))
    LATEST_SIMULATED_TIME = simulated_time
    PREDICTED_GATE_STATE, PREDICTED_UNASSIGNED_AIRCRAFT = predicted, unassigned
    LAST_SOLVER_SUMMARY = solver_summary
    LAST_DECISION_RESPONSE = copy.deepcopy(decisions)
    return decisions


def capture_view_snapshot() -> ViewSnapshot | None:
    """Take references to committed state; the feed builds its message outside the lock."""
    with STATE_LOCK:
        if STATE is None:
            return None
        return ViewSnapshot(
            state=STATE,
            assignments=ASSIGNMENTS,
            gate_starts=GATE_STARTS,
            assigned_at=ASSIGNED_AT,
            visit_positions=VISIT_POSITIONS,
            unassigned_reasons=UNASSIGNED_REASONS,
            deferred=DEFERRED_SESSIONS,
            airline_names=AIRLINE_NAMES,
        )


VIEW_FEED = ViewFeed(capture_view_snapshot, VIEW_CLOCK)


class EventRejected(Exception):
    """A webhook the matcher will not apply; retrying the same request cannot help."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _parse_stream_speed(headers) -> float | None:
    """X-Stream-Speed is sent for paced runs only; it just drives the dashboard clock."""
    try:
        speed = float(headers.get("X-Stream-Speed", ""))
    except ValueError:
        return None
    return speed if speed > 0 and speed != float("inf") else None


def _apply_event(action: str, message: dict[str, Any], run_id: str, index: int,
                 simulated_time: str, resync: bool) -> dict[str, Any]:
    """Apply one webhook event to matcher state and return its stream decisions."""
    global CURRENT_RUN_ID, LAST_EVENT_INDEX, LAST_EVENT_ID, MISSED_EVENTS
    event_id = f"{run_id}:{index}"
    if action == "InitializeAirport":
        if index != 0:
            raise EventRejected(400, "InitializeAirport must be event index 0.")
        decisions = _handle_initialize(message, simulated_time)
        CURRENT_RUN_ID, LAST_EVENT_INDEX, MISSED_EVENTS = run_id, 0, []
        LAST_EVENT_ID = event_id
        return decisions

    if STATE is None:
        raise EventRejected(409, "First message must be InitializeAirport.")
    if run_id != CURRENT_RUN_ID:
        raise EventRejected(409, "Run ID changed without a new InitializeAirport.")
    if index <= LAST_EVENT_INDEX:
        raise EventRejected(
            409, f"Event {index} was superseded: event {LAST_EVENT_INDEX} is already committed."
        )
    if index > LAST_EVENT_INDEX + 1:
        # The stream gave up on these. Their changes are lost to us, and their
        # responses may never have reached the stream, so resend the whole plan.
        missed = [LAST_EVENT_INDEX + 1, index - 1]
        print(f"EVENT GAP: stream skipped events {missed[0]}-{missed[1]}; resyncing.",
              file=sys.stderr, flush=True)
        MISSED_EVENTS = [*MISSED_EVENTS, missed]
        resync = True
    decisions = _handle_update(message, simulated_time, resync=resync)
    LAST_EVENT_INDEX = index
    LAST_EVENT_ID = event_id
    print(json.dumps({
        "event_id": event_id,
        "simulated_time": simulated_time,
        **decisions,
    }, separators=(",", ":")), flush=True)
    return decisions


def solver_status() -> str:
    return JULIA_WORKER.status if JULIA_WORKER is not None else "not_started"


class MatcherHandler(BaseHTTPRequestHandler):
    server_version = "SmartGateMatcher/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in VIEW_ENDPOINTS:
            # The passenger endpoint sends the operator's messages for now; see passenger/SPEC.md.
            VIEW_FEED.serve(self)
            return
        if path == "/health":
            with STATE_LOCK:
                _json_response(self, 200, {
                    "status": "ready" if STATE is not None else "waiting_for_initialization",
                    "solver": solver_status(),
                    "run_id": CURRENT_RUN_ID,
                    "last_event_id": LAST_EVENT_ID,
                    "missed_events": MISSED_EVENTS,
                    "deferred_tows": len(DEFERRED_SESSIONS),
                    "operator_websocket": "/operator/ws",
                    "passenger_websocket": "/passenger/ws",
                    "dashboard_clients": VIEW_FEED.client_count,
                    "operator_snapshot": "/assignments",
                })
            return
        if path == "/assignments":
            with STATE_LOCK:
                _json_response(self, 200, dashboard_snapshot())
            return
        _json_response(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:
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

        event_id = f"{run_id}:{index}"
        action = message["action"]
        resync = self.headers.get("X-Stream-Resync", "").strip().lower() == "true"
        with STATE_LOCK:
            cached = RESPONSE_CACHE.get(event_id)
            if cached is not None:
                # A retry, possibly of an event that finished after the stream timed
                # out. Return the committed answer; never apply an event twice.
                print(f"DUPLICATE event={event_id}: returning its committed response", flush=True)
                status, body = 200, cached
            else:
                saved = {name: globals()[name] for name in _EVENT_GLOBALS}
                try:
                    status, body = 200, _apply_event(action, message, run_id, index, simulated_time, resync)
                    if action == "InitializeAirport":
                        RESPONSE_CACHE.clear()
                    RESPONSE_CACHE[event_id] = body
                    while len(RESPONSE_CACHE) > RESPONSE_CACHE_SIZE:
                        RESPONSE_CACHE.popitem(last=False)
                    initializing = action == "InitializeAirport"
                    VIEW_CLOCK.observe(simulated_time, _parse_stream_speed(self.headers), reset=initializing)
                    VIEW_FEED.mark_changed(reset=initializing)
                except EventRejected as exc:
                    globals().update(saved)
                    print(f"REJECTED event={event_id}: {exc}", file=sys.stderr, flush=True)
                    status, body = exc.status, {"error": str(exc)}
                except TimezoneCoverageError as exc:
                    # Bad/incomplete schedule reference data is a client validation
                    # problem, not an internal server error. Nothing was committed.
                    globals().update(saved)
                    print(f"TIMEZONE COVERAGE event={event_id}: {exc}", file=sys.stderr, flush=True)
                    status, body = 422, {"error": str(exc), "error_type": "timezone_coverage"}
                except Exception as exc:
                    # Nothing was committed, so the stream may retry this event.
                    globals().update(saved)
                    print(f"ERROR event={event_id}: {exc}", file=sys.stderr, flush=True)
                    traceback.print_exc()
                    status, body = 500, {"error": str(exc)}
        try:
            _json_response(self, status, body)
        except (BrokenPipeError, ConnectionResetError):
            print(f"LATE RESPONSE event={event_id}: the stream stopped waiting; "
                  "a retry will receive the cached response", file=sys.stderr, flush=True)
            self.close_connection = True


def main() -> int:
    global JULIA_WORKER

    parser = argparse.ArgumentParser(
        description="SmartGate stream webhook matcher"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="HTTP port (default: 9000)",
    )
    args = parser.parse_args()

    # Start Julia before binding: until the solver is ready there is no listener,
    # so the stream cannot deliver anything (it retries InitializeAirport meanwhile).
    print("Starting the Julia solver; webhooks are not accepted until it is ready.", flush=True)
    JULIA_WORKER = JuliaWorker(
        SOLVER_BACKEND,
        JULIA,
        JULIA_TIMEOUT,
    )

    server = None
    try:
        server = ThreadingHTTPServer(
            (args.host, args.port),
            MatcherHandler,
        )
        VIEW_FEED.start()

        print(
            f"SmartGate matcher listening on "
            f"http://{args.host}:{args.port}/updates",
            flush=True,
        )
        print(
            f"Health: http://{args.host}:{args.port}/health | "
            f"assignments: http://{args.host}:{args.port}/assignments | "
            f"views: ws://{args.host}:{args.port}/operator/ws, "
            f"ws://{args.host}:{args.port}/passenger/ws",
            flush=True,
        )

        server.serve_forever()

    except KeyboardInterrupt:
        print("Stopping matcher.", flush=True)

    finally:
        VIEW_FEED.stop()
        if server is not None:
            server.server_close()

        if JULIA_WORKER is not None:
            JULIA_WORKER.close()
            JULIA_WORKER = None

        print("Julia solver stopped.", flush=True)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
