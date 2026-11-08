"""Live dashboard feeds for the operator and passenger views.

Both views read one full-state WebSocket message (see operator/SPEC.md and
passenger/SPEC.md on main). Changes are batched on a one-second tick; a tick
without changes sends a heartbeat so the views' clocks keep moving.

This module never imports matcher_server: the matcher runs as __main__, and a
second import would load a separate copy of its state. The matcher hands over
an immutable ViewSnapshot instead.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ENDPOINTS = ("/operator/ws", "/passenger/ws")
TICK_SECONDS = 1.0
SEND_TIMEOUT_SECONDS = 5.0
DEFAULT_TIMEZONE = ZoneInfo("America/Toronto")


@dataclass(frozen=True)
class ViewSnapshot:
    """References to committed matcher state. The matcher replaces, never mutates, these."""
    state: Any
    assignments: dict[str, str]
    gate_starts: dict[str, float]
    assigned_at: dict[str, str]
    visit_positions: dict[str, tuple[tuple[float, str, str | None], ...]]
    unassigned_reasons: dict[str, str]
    deferred: dict[str, str]
    airline_names: dict[str, str] = field(default_factory=dict)


class SimClock:
    """The simulated airport clock, extrapolated between webhook events.

    The stream only reports simulated time on each event. For paced runs it also
    sends its speed, so the clock can keep moving in quiet periods. It never moves
    backwards except when a new run initializes.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.anchor: datetime | None = None
        self.anchor_wall = 0.0
        self.speed = 0.0
        self.latest: datetime | None = None

    def observe(self, simulated_time: str, speed: float | None, *, reset: bool = False) -> None:
        value = datetime.fromisoformat(simulated_time.replace("Z", "+00:00"))
        with self.lock:
            if reset:
                self.latest = None
            self.anchor = value
            self.anchor_wall = time.monotonic()
            self.speed = speed or 0.0

    def now(self) -> datetime:
        with self.lock:
            if self.anchor is None:
                return datetime.now(timezone.utc)
            value = self.anchor + timedelta(seconds=(time.monotonic() - self.anchor_wall) * self.speed)
            if self.latest is not None and value < self.latest:
                value = self.latest
            self.latest = value
            return value


def _iso(value: datetime | None, tz: ZoneInfo) -> str | None:
    return value.astimezone(tz).isoformat(timespec="seconds") if value is not None else None


def empty_message(now: datetime, tz: ZoneInfo = DEFAULT_TIMEZONE) -> dict[str, Any]:
    return {"type": "assignments", "generatedAt": _iso(now, tz), "outages": [], "flights": [], "parkedAircraft": []}


def heartbeat_message(now: datetime, tz: ZoneInfo = DEFAULT_TIMEZONE) -> dict[str, Any]:
    return {"type": "heartbeat", "generatedAt": _iso(now, tz)}


def build_assignments_message(snapshot: ViewSnapshot, now: datetime) -> dict[str, Any]:
    """Build the operator message, a superset of the passenger message, without `previous`."""
    state = snapshot.state
    tz = state.airport_tz

    def at_minute(minute: float) -> str:
        # Matcher minutes are wall-clock minutes from the local midnight epoch.
        return (state.epoch + timedelta(minutes=float(minute))).replace(tzinfo=tz).isoformat(timespec="seconds")

    def from_utc(value: str | None) -> str | None:
        if not value:
            return None
        return _iso(datetime.fromisoformat(value.replace("Z", "+00:00")), tz)

    def gate_object(gate_id: str | None) -> dict[str, str] | None:
        record = state.gate_by_id.get(gate_id) if gate_id else None
        if record is None:
            return None
        if record.get("kind") == "remote":
            return {"terminal": "Remote", "gate": gate_id}
        terminal = str(record.get("terminal") or "")
        if terminal.startswith("T") and terminal[1:].isdigit():
            terminal = terminal[1:]
        return {"terminal": terminal, "gate": gate_id}

    sessions = state.build_sessions()
    # A YYZ->YYZ turnback ends one visit and starts the next; it is shown as an arrival.
    token_role: dict[str, tuple[str, str]] = {}
    for sid, session in sessions.items():
        if session.get("end_token"):
            token_role.setdefault(session["end_token"], (sid, "outbound"))
        # A merged visit (its departure was cancelled) has several arrivals.
        for token in session.get("leg_tokens") or (session.get("start_token"),):
            if token and token != session.get("end_token"):
                token_role[token] = (sid, "inbound")

    def placement(sid: str, role: str) -> tuple[str | None, dict[str, str] | None, str | None]:
        """Gate, occupancy and assignedAt for one half of a visit.

        A tow splits the visit: the inbound keeps the first gate until the tow,
        and the outbound holds the planned gate from then on.
        """
        session = sessions[sid]
        planned = snapshot.assignments.get(sid)
        entry = float(snapshot.gate_starts.get(sid, session["start"]))
        end = float(session["end"])
        segments = snapshot.visit_positions.get(sid, ())
        if len(segments) >= 2 and role == "inbound":
            gate, minute = segments[0][1], segments[1][0]
            return gate, {"start": at_minute(entry), "end": at_minute(minute)}, from_utc(segments[0][2])
        if planned is None:
            return None, None, None
        start = entry
        if len(segments) >= 2:
            start = max(entry, segments[-1][0])
        return planned, {"start": at_minute(start), "end": at_minute(end)}, from_utc(snapshot.assigned_at.get(sid))

    flights = []
    for route in state.routes.values():
        carrier = route["carrier"]
        for entry in route["legs"]:
            leg, token = entry["leg"], entry["token"]
            origin = str(leg.get("departureStation") or "").upper()
            destination = str(leg.get("arrivalStation") or "").upper()
            if "YYZ" not in (origin, destination):
                continue
            try:
                departure, arrival = state.leg_local_datetimes(leg)
            except Exception:
                departure = arrival = None
            cancelled = not entry["active"]
            sid, role = token_role.get(token, (None, None))
            gate_id = occupancy = assigned_at = None
            if sid is not None and not cancelled:
                gate_id, occupancy, assigned_at = placement(sid, role)

            number = str(leg.get("flightNumber") or "")
            codes = [f"{carrier}{number}{leg.get('flightSuffix') or ''}"]
            for share in leg.get("codeshare") or []:
                code = f"{share.get('carrier') or ''}{share.get('flightNumber') or ''}"
                if code and code not in codes:
                    codes.append(code)

            policy = state.token_policy.get(token, {})
            reason = str(policy.get("reason") or "").strip()
            emergency = policy.get("emergencyState")
            if emergency is None and policy.get("action") == "DivertFlight" and reason.lower() == "turnback":
                emergency = "Turnback"
            misc: dict[str, Any] = {}
            if reason:
                misc["reason"] = reason
            if not cancelled and gate_id is None:
                misc["waitlistReason"] = (
                    snapshot.unassigned_reasons.get(sid, "no_feasible_gate_slot") if sid is not None
                    else "no_ground_visit_for_this_aircraft"
                )
            if sid in snapshot.deferred:
                misc["gateBlocked"] = snapshot.deferred[sid]

            linked = None
            if sid is not None and not cancelled:
                other = sessions[sid].get("end_token" if role == "inbound" else "start_token")
                linked = other if other and other != token else None

            aircraft = entry["aircraft"]
            flights.append({
                "flightId": token,
                "flightCodes": codes,
                "airline": snapshot.airline_names.get(carrier) or carrier,
                "from": origin,
                "to": destination,
                "timeArrival": arrival.isoformat(timespec="seconds") if arrival else None,
                "timeDeparture": departure.isoformat(timespec="seconds") if departure else None,
                "gate": gate_object(gate_id),
                "cancelled": cancelled,
                "misc": misc,
                "registration": aircraft.get("registration") or None,
                "aircraftType": aircraft.get("aircraftType") or None,
                "emergencyState": emergency,
                "trafficType": leg.get("trafficType"),
                "occupancy": occupancy,
                "assignedAt": assigned_at,
                "linkedFlightId": linked,
            })

    def yyz_time(flight: dict[str, Any]) -> float:
        value = flight["timeArrival"] if flight["to"] == "YYZ" else flight["timeDeparture"]
        return datetime.fromisoformat(value).timestamp() if value else math.inf

    flights.sort(key=lambda flight: (yyz_time(flight), flight["flightId"]))

    parked = []
    for sid, session in sorted(sessions.items()):
        # Aircraft with a flight are shown through that flight's occupancy.
        if session.get("start_token") or session.get("end_token"):
            continue
        gate_id, occupancy, assigned_at = placement(sid, "outbound")
        parked.append({
            "carrier": session.get("carrier"),
            "registration": session.get("registration"),
            "aircraftType": session.get("aircraftType") or None,
            "gate": gate_object(gate_id),
            "occupancy": occupancy,
            "assignedAt": assigned_at,
        })

    outages = []
    for outage in state.outages:
        misc = {"reason": outage["reason"]} if outage.get("reason") else {}
        outages.append({
            "stand": outage["ifatc_stand"],
            "start": at_minute(outage["start"]),
            "end": None if outage.get("untilFurtherNotice") else at_minute(outage["end"]),
            "misc": misc,
        })

    return {
        "type": "assignments",
        "generatedAt": _iso(now, tz),
        "outages": outages,
        "flights": flights,
        "parkedAircraft": parked,
    }


class PreviousFields:
    """Remembers each flight's last sent fields, to fill `previous` with what changed."""

    def __init__(self):
        self.last: dict[str, dict[str, Any]] = {}
        self.previous: dict[str, dict[str, Any]] = {}

    def apply(self, flights: list[dict[str, Any]]) -> None:
        for flight in flights:
            flight_id = flight["flightId"]
            fields = {key: value for key, value in flight.items() if key not in ("flightId", "previous")}
            old = self.last.get(flight_id)
            previous = self.previous.get(flight_id, {})
            if old is not None:
                changed = {key: old.get(key) for key, value in fields.items() if old.get(key) != value}
                if changed:
                    previous = {**previous, **changed}
            self.last[flight_id] = fields
            self.previous[flight_id] = previous
            flight["previous"] = previous


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")


def ws_send(sock, payload: bytes, opcode: int = 1) -> None:
    length = len(payload)
    head = bytes([0x80 | opcode])
    if length < 126:
        head += bytes([length])
    elif length < 65536:
        head += bytes([126]) + struct.pack("!H", length)
    else:
        head += bytes([127]) + struct.pack("!Q", length)
    sock.sendall(head + payload)


def _read_exact(sock, length: int, *, idle_ok: bool) -> bytes | None:
    """Read exactly `length` bytes; a timeout before the first byte is raised only if idle_ok."""
    chunks = bytearray()
    while len(chunks) < length:
        try:
            part = sock.recv(length - len(chunks))
        except TimeoutError:
            if idle_ok and not chunks:
                raise
            continue
        if not part:
            return None
        chunks.extend(part)
    return bytes(chunks)


def ws_read_frame(sock) -> tuple[int | None, bytes]:
    """Read one client frame. Raises TimeoutError only while idle between frames."""
    header = _read_exact(sock, 2, idle_ok=True)
    if header is None:
        return None, b""
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        extended = _read_exact(sock, 2, idle_ok=False)
        if extended is None:
            return None, b""
        length = struct.unpack("!H", extended)[0]
    elif length == 127:
        extended = _read_exact(sock, 8, idle_ok=False)
        if extended is None:
            return None, b""
        length = struct.unpack("!Q", extended)[0]
    masked = bool(header[1] & 0x80)
    mask = _read_exact(sock, 4, idle_ok=False) if masked else None
    payload = _read_exact(sock, length, idle_ok=False) if length else b""
    if payload is None or (masked and mask is None):
        return None, b""
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _refuse(handler, status: int, error: str) -> None:
    body = encode({"error": error})
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ViewFeed:
    """Publishes the full assignments state to every dashboard WebSocket once per tick."""

    def __init__(self, source: Callable[[], ViewSnapshot | None], clock: SimClock,
                 tick_seconds: float = TICK_SECONDS):
        self.source = source
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.lock = threading.Lock()
        self.clients: dict[Any, threading.Lock] = {}
        self.version = 0
        self.published_version = -1
        self.reset_pending = False
        self.previous = PreviousFields()
        self.current: bytes | None = None
        self.tz = DEFAULT_TIMEZONE
        self.stopped = threading.Event()
        # Serializes publishing: the ticker and newly connected clients both publish.
        self.publish_lock = threading.Lock()

    def mark_changed(self, *, reset: bool = False) -> None:
        """Record a committed change; `reset` starts a new day's `previous` history."""
        with self.lock:
            self.version += 1
            self.reset_pending = self.reset_pending or reset

    def start(self) -> None:
        threading.Thread(target=self._run, name="view-feed", daemon=True).start()

    def stop(self) -> None:
        self.stopped.set()

    def _run(self) -> None:
        while not self.stopped.wait(self.tick_seconds):
            try:
                self.publish()
            except Exception:
                # A bad snapshot must not stop the feed; the next change retries.
                print("VIEW FEED ERROR:", file=sys.stderr, flush=True)
                traceback.print_exc()

    def publish(self, *, heartbeat: bool = True) -> None:
        """Send the full state if it changed, else a heartbeat (unless heartbeat=False)."""
        with self.publish_lock:
            self._publish(heartbeat)

    def _publish(self, heartbeat: bool) -> None:
        with self.lock:
            version, reset = self.version, self.reset_pending
            self.reset_pending = False
            changed = version != self.published_version
        if not changed and not heartbeat:
            return
        now = self.clock.now()
        if changed:
            if reset:
                self.previous = PreviousFields()
            snapshot = self.source()
            if snapshot is None:
                message = empty_message(now, self.tz)
            else:
                self.tz = snapshot.state.airport_tz
                message = build_assignments_message(snapshot, now)
                self.previous.apply(message["flights"])
            data = encode(message)
            with self.lock:
                self.current = data
                self.published_version = version
                clients = list(self.clients.items())
        else:
            data = encode(heartbeat_message(now, self.tz))
            with self.lock:
                clients = list(self.clients.items())
        for sock, send_lock in clients:
            with send_lock:
                try:
                    ws_send(sock, data)
                except OSError:
                    self._drop(sock)

    def _drop(self, sock) -> None:
        with self.lock:
            self.clients.pop(sock, None)

    @property
    def client_count(self) -> int:
        with self.lock:
            return len(self.clients)

    def serve(self, handler) -> None:
        """Upgrade an HTTP request to a WebSocket and keep it subscribed until it closes."""
        handler.close_connection = True
        if handler.headers.get("Upgrade", "").lower() != "websocket":
            _refuse(handler, 426, "WebSocket upgrade required")
            return
        if handler.headers.get("Sec-WebSocket-Version") != "13":
            _refuse(handler, 400, "WebSocket version 13 is required")
            return
        key = handler.headers.get("Sec-WebSocket-Key", "").strip()
        if not key:
            _refuse(handler, 400, "Missing Sec-WebSocket-Key")
            return
        accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.wfile.flush()

        # Bring the shared state up to date so a new client never starts stale.
        self.publish(heartbeat=False)
        sock = handler.connection
        sock.settimeout(SEND_TIMEOUT_SECONDS)
        send_lock = threading.Lock()
        # Register and take the current state together, holding this client's send
        # lock, so the next tick's message can only follow the initial one.
        with self.lock:
            self.clients[sock] = send_lock
            data = self.current
            send_lock.acquire()
        try:
            ws_send(sock, data if data is not None else encode(empty_message(self.clock.now(), self.tz)))
        except OSError:
            self._drop(sock)
            return
        finally:
            send_lock.release()

        try:
            while not self.stopped.is_set():
                try:
                    opcode, payload = ws_read_frame(sock)
                except TimeoutError:
                    continue
                if opcode is None:
                    break
                if opcode == 8:
                    with send_lock:
                        ws_send(sock, payload[:2], opcode=8)
                    break
                if opcode == 9:
                    with send_lock:
                        ws_send(sock, payload, opcode=10)
        except OSError:
            pass
        finally:
            self._drop(sock)
