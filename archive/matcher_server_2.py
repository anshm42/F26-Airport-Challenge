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
from collections import defaultdict
from datetime import date, datetime, timedelta, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
JULIA_TIMEOUT = float(os.environ.get("GMS_JL_TIMEOUT", "9.0"))
WIDTH_CLASSES = ("A", "B", "C", "D", "E", "F")

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


def load_references() -> tuple[dict[str, str], list[dict[str, str]], str]:
    if not AIRCRAFT_JSON.is_file():
        raise FileNotFoundError(f"Aircraft reference not found: {AIRCRAFT_JSON}")
    if not PEARSON_JSON.is_file():
        raise FileNotFoundError(f"Pearson stand reference not found: {PEARSON_JSON}")

    aircraft_data = json.loads(AIRCRAFT_JSON.read_text(encoding="utf-8"))
    pearson_data = json.loads(PEARSON_JSON.read_text(encoding="utf-8"))

    aircraft_classes: dict[str, str] = {}
    for record in aircraft_data.get("aircraftTypes", {}).get("records", []):
        code = norm(record.get("aircraftType"))
        width_class = norm(record.get("widthClass"))
        if code and width_class in WIDTH_CLASSES:
            aircraft_classes[code] = width_class

    closed_stands = {
        norm(item.get("name"))
        for item in pearson_data.get("knownStandExceptions", [])
        if norm(item.get("status")) == "CLOSED"
    }

    gates: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in pearson_data.get("ifatcStands", {}).get("records", []):
        name = str(record.get("name", "")).strip()
        width_class = norm(record.get("widthClass"))
        key = norm(name)

        # Ordinary passenger flights use named gates. Remote stands are kept
        # as a separate resource class and are enabled only for eligible diversions.
        is_gate = key.startswith("GATE ")
        is_remote = key.startswith("REMOTE STAND ")
        if not (is_gate or is_remote):
            continue
        if key in closed_stands or width_class not in WIDTH_CLASSES or key in seen:
            continue
        seen.add(key)
        gates.append({"id": name, "widthClass": width_class, "kind": "remote" if is_remote else "gate"})

    gates.sort(key=lambda item: item["id"])
    if not gates:
        raise RuntimeError("No usable named gates were found in pearson.json.")
    airport_timezone = pearson_data.get("airport", {}).get("timeZone")
    if not airport_timezone:
        raise ValueError("pearson.json is missing airport.timeZone.")
    return aircraft_classes, gates, str(airport_timezone)


class ScenarioState:
    def __init__(self, scenario: dict[str, Any], aircraft_classes: dict[str, str],
                 gates: list[dict[str, str]], airport_timezone: str):
        self.scenario = scenario
        self.aircraft_classes = aircraft_classes
        self.gates = gates
        self.airport_tz = ZoneInfo(airport_timezone)
        if not STATION_TZ_JSON.is_file():
            raise FileNotFoundError(f"Station timezone reference not found: {STATION_TZ_JSON}")
        timezone_data = json.loads(STATION_TZ_JSON.read_text(encoding="utf-8"))
        self.station_timezones = {
            norm(code): ZoneInfo(zone)
            for code, zone in timezone_data.get("timeZones", {}).items()
        }
        self.gate_ids = {norm(g["id"]): g["id"] for g in gates}

        # Passenger-facing gate labels are a separate naming system in
        # pearson.json. Resolve them from passengerGateGroups without guessing
        # a relationship to IFATC stand names.
        pearson_data = json.loads(PEARSON_JSON.read_text(encoding="utf-8"))
        self.passenger_gate_groups: dict[str, dict[str, Any]] = {}
        terminals = (
            pearson_data.get("passengerGateGroups", {})
            .get("terminals", {})
        )
        for terminal, terminal_data in terminals.items():
            for group in terminal_data.get("groups", []):
                labels = [str(label).strip() for label in group if str(label).strip()]
                group_record = {
                    "terminal": str(terminal),
                    "labels": labels,
                }
                for label in labels:
                    self.passenger_gate_groups[norm(label)] = group_record

        self.routes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.parked: dict[str, dict[str, str]] = {}
        self.outages: list[dict[str, Any]] = []
        self.unmatched_outages: list[dict[str, Any]] = []
        self.recognized_passenger_gate_outages: list[dict[str, Any]] = []
        self.token_policy: dict[str, dict[str, Any]] = {}
        self.epoch: datetime | None = None
        self.horizon_end: datetime | None = None
        self.initialized = False

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

        for flight_message in message.get("flights", []):
            action = flight_message.get("action")
            if action != "CreateFlight":
                raise ValueError(
                    f"InitializeAirport.flights must contain CreateFlight messages; got {action!r}."
                )
            self._create_route(flight_message)

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
                "token": token,
            })
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
            # Aircraft details are present in the webhook and remain shared
            # for the updated leg.
            if flight:
                entry["aircraft"] = dict(flight)

        elif action == "CancelFlight":
            scope = norm((message.get("cancelScope") or {}).get("type"))
            if scope == "ALLLEGS":
                for item in self.routes[route_id]["legs"]:
                    item["active"] = False
            else:
                entry["active"] = False

        elif action == "ReinstateFlight":
            for item in self.routes[route_id]["legs"]:
                item["active"] = True

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

        # First recognize an exact IFATC stand identifier. Otherwise check the
        # passenger-facing labels and alias groups explicitly listed in
        # pearson.json. Never synthesize a name or infer a stand crosswalk.
        ifatc_stand_id = self.gate_ids.get(gate_key)
        passenger_group = self.passenger_gate_groups.get(gate_key)

        outage_date = event_local.date()
        start_dt = datetime.combine(outage_date, parse_hhmm(message["scheduledStartTime"]))
        end_raw = message.get("scheduledEndTime")
        if end_raw:
            end_dt = datetime.combine(outage_date, parse_hhmm(end_raw))
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
        else:
            end_dt = self.horizon_end or (start_dt + timedelta(days=1))

        outage_record = {
            # Keep the exact identifier supplied by the event/JSON naming
            # system. The solver only consumes IFATC stand identifiers.
            "gate": ifatc_stand_id if ifatc_stand_id is not None else gate_raw,
            "start": minutes_from_epoch(start_dt, self.epoch),
            "end": minutes_from_epoch(end_dt, self.epoch),
        }

        if ifatc_stand_id is not None:
            outage_record["lookup_status"] = "matched_ifatc_stand"
            outage_record["resource_type"] = "ifatc_stand"
            self.outages.append(outage_record)
        elif passenger_group is not None:
            passenger_record = {
                **outage_record,
                "gate": gate_raw,
                "lookup_status": "matched_passenger_gate_label",
                "resource_type": "passenger_gate_group",
                "terminal": passenger_group["terminal"],
                "passenger_gate_group": passenger_group["labels"],
                "reason": (
                    "The label exists in pearson.json passengerGateGroups. "
                    "That section does not identify the corresponding IFATC "
                    "stand resource, so this outage cannot yet be applied "
                    "to an optimizer stand."
                ),
            }
            self.recognized_passenger_gate_outages.append(passenger_record)
        else:
            self.unmatched_outages.append({
                **outage_record,
                "lookup_status": "unknown_gate_label",
                "reason": (
                    "The gate identifier was not found in either "
                    "ifatcStands.records or passengerGateGroups.terminals "
                    "in pearson.json."
                ),
            })

    def _leg_datetimes(self, leg: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        # The contract gives local departure and arrival times at their
        # respective stations. Convert through UTC so date-line and DST
        # differences do not create false YYZ parking intervals.
        day = date.fromisoformat(leg["originatingDate"])
        dep_station = norm(leg.get("departureStation"))
        arr_station = norm(leg.get("arrivalStation"))
        if dep_station != "YYZ" and arr_station != "YYZ":
            return None, None
        dep_tz = self.station_timezones.get(dep_station)
        arr_tz = self.station_timezones.get(arr_station)
        if dep_tz is None or arr_tz is None:
            raise RuntimeError(
                f"No IANA timezone mapping for YYZ-relevant route {dep_station}->{arr_station}; "
                f"add the station to {STATION_TZ_JSON.name}."
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
                                sessions, anchor, registration, meta, start_dt, item["time"]
                            )
                        opened = None

            if opened is not None:
                start_dt, anchor, meta = opened
                if horizon > start_dt:
                    self._add_session(
                        sessions, anchor, registration, meta, start_dt, horizon
                    )

        return sessions

    def _add_session(self, sessions, anchor, registration, aircraft, start, end):
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
            "aircraftType": aircraft_type,
            "widthClass": width_class,
            "start": minutes_from_epoch(start, self.epoch),
            "end": minutes_from_epoch(end, self.epoch),
            "priority": priority,
            "remoteAllowed": remote_allowed,
            "turnback": turnback,
        }

    def outage_payload(self) -> dict[str, list[list[float]]]:
        # The JuMP model's resources are IFATC stand IDs. Passenger-facing
        # labels are recognized and reported separately, but must not be
        # passed as if they were physical stand IDs.
        result: dict[str, list[list[float]]] = defaultdict(list)
        for outage in self.outages:
            if outage.get("resource_type") != "ifatc_stand":
                continue
            result[outage["gate"]].append([outage["start"], outage["end"]])
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
    gates: list[dict[str, str]],
) -> tuple[set[str], list[str]]:
    changed = {
        sid for sid, session in after.items()
        if sid not in before or before[sid] != session
    }
    removed = [sid for sid in before if sid not in after]

    by_id = after
    # Any session whose currently assigned gate is now closed or too small is
    # affected, even if its own schedule fields did not change.
    gate_classes = {norm(g["id"]): g["widthClass"] for g in gates}
    for sid, gate in assignments.items():
        if sid not in by_id:
            continue
        session = by_id[sid]
        gate_class = gate_classes.get(norm(gate))
        ac_class = session.get("widthClass")
        if not allowed_width(ac_class, gate_class):
            changed.add(sid)
            continue
        for start, end in outages.get(gate, []):
            if float(session["start"]) < float(end) and float(start) < float(session["end"]):
                changed.add(sid)
                break

    return changed, removed


def allowed_width(aircraft_class: str | None, gate_class: str | None) -> bool:
    if aircraft_class not in WIDTH_CLASSES or gate_class not in WIDTH_CLASSES:
        return False
    a = WIDTH_CLASSES.index(aircraft_class)
    g = WIDTH_CLASSES.index(gate_class)
    return g == a or g == a + 1



def repair_assignments_python(
    sessions: dict[str, dict[str, Any]],
    gates: list[dict[str, str]],
    outages: dict[str, list[list[float]]],
    previous: dict[str, str],
    changed: set[str],
) -> dict[str, Any]:
    """Greedy local repair; unaffected assignments are fixed bookings."""
    by_id = sessions
    occupied: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for sid, gate_id in previous.items():
        if sid in changed or sid not in by_id:
            continue
        session = by_id[sid]
        occupied[gate_id].append((float(session["start"]), float(session["end"])))

    gate_index = {str(g["id"]): i for i, g in enumerate(gates)}

    def candidates_for(session: dict[str, Any]) -> list[dict[str, Any]]:
        aircraft_class = session.get("widthClass")
        if aircraft_class not in WIDTH_CLASSES:
            return []
        ai = WIDTH_CLASSES.index(aircraft_class)
        result = []
        for gate in gates:
            kind = gate.get("kind", "gate")
            if kind == "remote" and not session.get("remoteAllowed", False):
                continue
            gate_class = gate.get("widthClass")
            if gate_class not in WIDTH_CLASSES:
                continue
            gi = WIDTH_CLASSES.index(gate_class)
            if gi not in (ai, ai + 1):
                continue
            gid = str(gate["id"])
            start, end = float(session["start"]), float(session["end"])
            if any(start < float(w[1]) and float(w[0]) < end for w in outages.get(gid, [])):
                continue
            result.append(gate)
        return result

    decision_ids = [sid for sid in changed if sid in by_id]
    candidate_map = {sid: candidates_for(by_id[sid]) for sid in decision_ids}
    decision_ids.sort(key=lambda sid: (
        0 if by_id[sid].get("priority", False) else 1,
        len(candidate_map[sid]),
        0 if sid in previous else 1,
        sid,
    ))

    new_assignments: list[list[str]] = []
    reassignments: list[list[str]] = []
    unassigned: list[str] = []
    for sid in decision_ids:
        session = by_id[sid]
        old_gate = previous.get(sid)
        candidates = candidate_map[sid]

        def cost(gate: dict[str, Any]) -> tuple[float, str]:
            same = session.get("widthClass") == gate.get("widthClass")
            value = 0.0 if same else 500.0
            is_remote = gate.get("kind") == "remote"
            turnback = bool(session.get("turnback", False))
            if is_remote:
                value += 5000.0 if turnback else 0.0
            elif session.get("remoteAllowed", False) and not turnback:
                value += 1000.0
            value += gate_index[str(gate["id"])] * 0.001
            if old_gate is not None and str(gate["id"]) == old_gate:
                value -= 1_000_000.0
            return value, str(gate["id"])

        selected = None
        start, end = float(session["start"]), float(session["end"])
        for gate in sorted(candidates, key=cost):
            gid = str(gate["id"])
            conflict = any(start < other_end and other_start < end
                           for other_start, other_end in occupied[gid])
            if conflict:
                continue
            selected = gid
            occupied[gid].append((start, end))
            break

        if selected is None:
            unassigned.append(sid)
        elif old_gate is None:
            new_assignments.append([sid, selected])
        elif selected != old_gate:
            reassignments.append([sid, selected])

    return {"assignments": new_assignments, "reassignments": reassignments, "unassigned": unassigned}

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
CURRENT_RUN_ID: str | None = None
LAST_EVENT_INDEX = -1
LAST_EVENT_ID: str | None = None
STATE_LOCK = threading.RLock()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
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


def _handle_initialize(message: dict[str, Any], simulated_time: str) -> dict[str, Any]:
    global STATE, ASSIGNMENTS
    aircraft_classes, gates, airport_timezone = load_references()
    candidate = ScenarioState({}, aircraft_classes, gates, airport_timezone)
    candidate.initialize({"timestamp": simulated_time, "message": message})
    sessions = candidate.build_sessions()
    payload = build_solver_payload(sessions, gates, candidate.outage_payload())
    payload["mode"] = "initial"
    result = run_julia(SOLVER_BACKEND, payload)
    assignments = assignment_map(result.get("assignments", []))
    STATE = candidate
    ASSIGNMENTS = assignments
    return {
        "ready": True,
        "sessions": len(sessions),
        "assigned": len(assignments),
        "unassigned": result.get("unassigned", []),
        "unmatched_gate_outages": candidate.unmatched_outages,
        "recognized_passenger_gate_outages": candidate.recognized_passenger_gate_outages,
    }


def _handle_update(message: dict[str, Any], simulated_time: str) -> dict[str, Any]:
    global STATE, ASSIGNMENTS
    if STATE is None or not STATE.initialized:
        raise RuntimeError("Matcher has not received InitializeAirport.")
    candidate = copy.deepcopy(STATE)
    before = candidate.build_sessions()
    action = candidate.apply_message({"timestamp": simulated_time, "message": message})
    _refresh_horizon(candidate)
    after = candidate.build_sessions()
    outages = candidate.outage_payload()
    assignments = dict(ASSIGNMENTS)

    changed, removed = determine_changed_sessions(before, after, assignments, outages, candidate.gates)
    for sid in removed:
        assignments.pop(sid, None)
    for sid in list(assignments):
        if sid not in after:
            assignments.pop(sid, None)

    solver_result: dict[str, Any] = {"assignments": [], "reassignments": [], "unassigned": []}
    if changed:
        # Updates are deliberately repaired in-process; only the initial
        # full-schedule solve launches Julia/Gurobi. This keeps webhook replies
        # comfortably inside the updater's 10-second acknowledgment window.
        solver_result = repair_assignments_python(after, candidate.gates, outages, assignments, changed)
        for sid in solver_result.get("unassigned", []):
            assignments.pop(str(sid), None)
        for pair in solver_result.get("assignments", []):
            assignments[str(pair[0])] = str(pair[1])
        for pair in solver_result.get("reassignments", []):
            assignments[str(pair[0])] = str(pair[1])

    # Commit only after the solver succeeds; a failed update leaves the prior
    # state intact and returns a non-2xx response to the updater.
    STATE = candidate
    ASSIGNMENTS = assignments
    return {
        "action": action,
        "changed_sessions": sorted(changed),
        "removed_sessions": removed,
        "assignments": solver_result.get("assignments", []),
        "reassignments": solver_result.get("reassignments", []),
        "unassigned": solver_result.get("unassigned", []),
        "unmatched_gate_outages": candidate.unmatched_outages,
        "recognized_passenger_gate_outages": candidate.recognized_passenger_gate_outages,
    }


class MatcherHandler(BaseHTTPRequestHandler):
    server_version = "SmartGateMatcher/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
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
                _json_response(self, 200, {
                    "run_id": CURRENT_RUN_ID,
                    "last_event_id": LAST_EVENT_ID,
                    "assignments": ASSIGNMENTS,
                    "unassigned_sessions": sorted(
                        sid for sid in (STATE.build_sessions() if STATE else {})
                        if sid not in ASSIGNMENTS
                    ),
                    "unmatched_gate_outages": STATE.unmatched_outages if STATE else [],
                    "recognized_passenger_gate_outages": (
                        STATE.recognized_passenger_gate_outages if STATE else []
                    ),
                })
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
                    _json_response(self, 200, {"status": "initialized", **summary})
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
                print(json.dumps({
                    "event_id": LAST_EVENT_ID,
                    "simulated_time": simulated_time,
                    **summary,
                }, separators=(",", ":")), flush=True)
                _empty_response(self, 204)
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
