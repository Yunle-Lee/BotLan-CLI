#!/usr/bin/env python3
"""Framed line-JSON protocol shared by the daemon socket and the approve CLI.

Ported from proxysoul/Empryo src/hearth/protocol.ts: one request per line, one response per line,
UTF-8, the socket closed after the response is flushed - this is RPC, not streaming. Same three
hardening details as the original: a 1 MiB hard cap per frame, an idle read timeout so a peer that
never finishes a frame cannot hold the connection, and a protocol version check that fails closed.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Callable, Optional

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024          # 1 MiB hard cap per frame
IDLE_TIMEOUT_S = 30.0


class ProtocolError(RuntimeError):
    pass


def write_frame(sock: socket.socket, response: dict) -> None:
    try:
        sock.sendall((json.dumps(response) + "\n").encode())
    except OSError:
        pass                            # peer already gone - drop it silently, as the original does


def read_frame(sock: socket.socket, max_bytes: int = MAX_FRAME_BYTES,
               timeout: float = IDLE_TIMEOUT_S) -> dict:
    """Read exactly one frame. Raises ProtocolError on oversize, timeout or version mismatch."""
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise ProtocolError("socket idle timeout") from None
        if not chunk:
            raise ProtocolError("peer closed before a complete frame")
        buf += chunk
        if len(buf) > max_bytes:
            raise ProtocolError(f"frame exceeds {max_bytes} bytes")
    line = buf.split(b"\n", 1)[0]
    try:
        req = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"bad frame: {exc}") from None
    if not isinstance(req, dict) or req.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("protocol version mismatch")
    return req


def socket_request(path: str, req: dict, timeout_ms: int = 5000) -> dict:
    """Connect, send one request, await one response, close - the port of socketRequest()."""
    req = dict(req, v=PROTOCOL_VERSION)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_ms / 1000)
    try:
        sock.connect(path)
    except OSError as exc:
        raise ProtocolError(f"cannot reach {path}: {exc}") from None
    try:
        sock.sendall((json.dumps(req) + "\n").encode())
        return read_frame(sock, timeout=timeout_ms / 1000)
    except ProtocolError:
        raise
    finally:
        try:
            sock.close()
        except OSError:
            pass


class SocketServer:
    """The daemon side: one thread per connection, one frame in, one frame out, then close."""

    def __init__(self, path: str, handler: Callable[[dict], dict]) -> None:
        self.path = path
        self.handler = handler
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        path = os.path.expanduser(self.path)
        if os.path.exists(path):
            os.unlink(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, 0o600)           # owner-only: approving a tool call is not a public endpoint
        self._sock.listen(16)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            try:
                req = read_frame(conn)
            except ProtocolError as exc:
                write_frame(conn, {"v": PROTOCOL_VERSION, "error": str(exc)})
                return
            try:
                write_frame(conn, dict(self.handler(req), v=PROTOCOL_VERSION))
            except Exception as exc:              # a handler bug must not kill the server
                write_frame(conn, {"v": PROTOCOL_VERSION, "error": f"{type(exc).__name__}: {exc}"})

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(os.path.expanduser(self.path))
        except OSError:
            pass
