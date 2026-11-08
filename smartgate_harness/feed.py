"""Minimal WebSocket client for a matcher's passenger feed."""

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from urllib.parse import urlsplit


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FeedListener(threading.Thread):
    """Calls `on_message(received_at, payload)` for each text message, timed on arrival."""

    def __init__(self, url, on_message):
        super().__init__(daemon=True, name=f"feed-{url}")
        self.url = url
        self.on_message = on_message
        self.connected = threading.Event()
        self.error = None
        self.sock = None

    def run(self):
        try:
            reader = self._connect()
            self.connected.set()
            fragments = []
            while True:
                header = reader.read(2)
                if len(header) < 2:
                    return
                opcode, length = header[0] & 0x0F, header[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", reader.read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", reader.read(8))[0]
                payload = reader.read(length)
                received = time.perf_counter()
                if opcode == 8:
                    self._send(8)
                    return
                if opcode == 9:
                    self._send(10, payload)
                elif opcode in (0, 1):
                    fragments.append(payload)
                    if header[0] & 0x80:
                        self.on_message(received, b"".join(fragments))
                        fragments = []
        except (OSError, ValueError) as error:
            self.error = error
        finally:
            self.connected.set()

    def _connect(self):
        parts = urlsplit(self.url)
        if parts.scheme != "ws":
            raise ValueError(f"feed URL must use ws://: {self.url}")
        port = parts.port or 80
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock = socket.create_connection((parts.hostname, port), timeout=10)
        self.sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {parts.hostname}:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        reader = self.sock.makefile("rb")
        status = reader.readline()
        headers = {}
        while (line := reader.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        if b" 101 " not in status or headers.get("sec-websocket-accept") != expected:
            raise ValueError(f"WebSocket handshake failed: {status.decode('latin-1').strip()}")
        self.sock.settimeout(None)
        return reader

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(struct.pack("!BB", 0x80 | opcode, 0x80 | len(payload)) + mask + masked)

    def close(self):
        if self.sock is None:
            return
        try:
            self._send(8)
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.join(5)
