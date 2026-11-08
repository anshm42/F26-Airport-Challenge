"""Webhook receiver and passenger WebSocket feed for the greedy baseline.

Speaks the same contracts as the matcher: stream webhooks at `POST /updates`,
the full-list passenger feed at `/passenger/ws`, and readiness at `/health`.
"""

import base64
import hashlib
import json
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .planner import NaiveMatcher


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY_BYTES = 10_000_000


def ws_send(sock, payload, opcode=1):
    length = len(payload)
    if length < 126:
        head = struct.pack("!BB", 0x80 | opcode, length)
    elif length < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, length)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, length)
    sock.sendall(head + payload)


def ws_read_frame(reader):
    header = reader.read(2)
    if len(header) < 2:
        return None, b""
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", reader.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", reader.read(8))[0]
    mask = reader.read(4) if header[1] & 0x80 else None
    payload = reader.read(length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header[0] & 0x0F, payload


class NaiveServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.matcher = NaiveMatcher()
        self.lock = threading.Lock()
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.last_flights = None
        self.last_event_id = None
        super().__init__(address, NaiveHandler)

    def board_bytes(self):
        if self.matcher.state is None:
            return json.dumps({"type": "assignments", "generatedAt": None, "flights": []}).encode("utf-8"), None
        board = self.matcher.board()
        return (json.dumps(board, separators=(",", ":"), allow_nan=False).encode("utf-8"),
                json.dumps(board["flights"], separators=(",", ":"), allow_nan=False))

    def broadcast(self, *, force=False):
        payload, flights = self.board_bytes()
        if not force and flights == self.last_flights:
            return
        self.last_flights = flights
        with self.clients_lock:
            clients = list(self.clients)
        for sock in clients:
            try:
                ws_send(sock, payload)
            except OSError:
                with self.clients_lock:
                    self.clients.discard(sock)


class NaiveHandler(BaseHTTPRequestHandler):
    server_version = "SmartGateNaive/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def respond(self, status, body):
        data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/passenger/ws":
            self.serve_websocket()
        elif path == "/health":
            with self.server.lock:
                ready = self.server.matcher.state is not None
                self.respond(200, {"status": "ready" if ready else "waiting_for_initialization",
                                   "last_event_id": self.server.last_event_id})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        if urlsplit(self.path).path != "/updates":
            self.respond(404, {"error": "POST endpoint must be /updates"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("request body is empty or too large")
            message = json.loads(self.rfile.read(length))
            simulated_time = self.headers["X-Stream-Simulated-Time"]
            event_id = self.headers["X-Stream-Event-Id"]
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            self.respond(400, {"error": str(error)})
            return
        with self.server.lock:
            try:
                started = time.perf_counter()
                decisions = self.server.matcher.handle(message, simulated_time)
                computed = time.perf_counter() - started
                self.server.last_event_id = event_id
                self.server.broadcast(force=message["action"] == "InitializeAirport")
            except Exception as error:
                print(f"ERROR event={event_id}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
                self.respond(500, {"error": f"{type(error).__name__}: {error}"})
                return
            print(json.dumps({
                "event_id": event_id, "action": message["action"], "simulated_time": simulated_time,
                "compute_seconds": round(computed, 6), "assignments": len(decisions["assignments"]),
                "moves": len(decisions["moves"]), "waitlisted": len(self.server.matcher.waitlist),
            }, separators=(",", ":")), flush=True)
            self.respond(200, decisions)

    def serve_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self.respond(426, {"error": "WebSocket upgrade required"})
            return
        accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        sock = self.connection
        try:
            # Snapshot and subscribe together so no update slips between them.
            with self.server.lock:
                ws_send(sock, self.server.board_bytes()[0])
                with self.server.clients_lock:
                    self.server.clients.add(sock)
            while True:
                opcode, payload = ws_read_frame(self.rfile)
                if opcode is None or opcode == 8:
                    break
                if opcode == 9:
                    ws_send(sock, payload, opcode=10)
        except OSError:
            pass
        finally:
            with self.server.clients_lock:
                self.server.clients.discard(sock)
            self.close_connection = True
