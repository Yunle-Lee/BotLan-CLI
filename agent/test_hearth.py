#!/usr/bin/env python3
"""Checks for the ported hearth layer: approvals, the socket protocol, surfaces and tab loops.

Mirrors the intent of the two upstream tests (tests/hearth-tui-host.test.ts, tests/hearth-tui-actions
.test.ts) plus the parts that only exist because this runs in one process: the sweeper, the frame
caps, and the parked-approval path.

Run: python3 agent/test_hearth.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.approvals import MAX_PENDING, ApprovalPolicy, ApprovalRegistry
from agent.protocol import (IDLE_TIMEOUT_S, MAX_FRAME_BYTES, PROTOCOL_VERSION, ProtocolError,
                            SocketServer, read_frame, socket_request)
from agent.surface import ApprovalUI, SseSurface, SurfaceHost
from agent.tab_loop import MAX_QUEUED_PROMPTS, TabLoop, TabRegistry

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def test_registry():
    reg = ApprovalRegistry(sweep_interval_s=60)
    seen = []
    entry = reg.register(session_id="s1", tool_name="write_file", tool_call_id="c1", cwd="/tmp",
                         summary="write out.txt", resolve=seen.append)
    check("register returns an id and a public view", bool(entry.id) and entry.public()["tool_name"] == "write_file")
    check("resolve returns True once", reg.resolve(entry.id, "allow", "looks fine") is True)
    check("resolve returns False the second time", reg.resolve(entry.id, "allow") is False)
    check("the resolver saw the decision", seen and seen[0]["decision"] == "allow", str(seen))

    timed = []
    t = reg.register(session_id="s1", tool_name="shell", tool_call_id="c2", cwd="/tmp",
                     timeout_ms=10, resolve=timed.append)
    time.sleep(0.05)
    check("expired approval is swept and denied",
          reg.sweep_expired() == 1 and timed and timed[0]["decision"] == "deny"
          and timed[0]["reason"] == "approval timed out", str(timed))

    cancel = []
    for i in range(3):
        reg.register(session_id="s2", tool_name="shell", tool_call_id=f"c{i}", cwd="/tmp",
                     resolve=cancel.append)
    check("cancel_for_session denies that session only", reg.cancel_for_session("s2") == 3
          and all(x["reason"] == "session ended" for x in cancel), str(cancel))

    full = ApprovalRegistry(sweep_interval_s=60)
    denied = []
    for i in range(MAX_PENDING):
        full.register(session_id="s3", tool_name="x", tool_call_id=str(i), cwd="/tmp",
                      resolve=denied.append)
    overflow = full.register(session_id="s3", tool_name="x", tool_call_id="over", cwd="/tmp",
                             resolve=denied.append)
    check("registry full denies the new request instead of evicting a waiter",
          len(full.list()) == MAX_PENDING and denied[-1]["reason"] == "approval registry full",
          str(denied[-1]))
    check("overflow entry is not stored", full.get(overflow.id) is None)
    full.stop()
    reg.stop()


def test_policy():
    p = ApprovalPolicy(auto_approve=["read_file"], auto_deny=["shell"], switch_on=True)
    check("auto-approve allows", p.decide("read_file") == "allow")
    check("auto-deny denies", p.decide("shell") == "deny")
    check("anything else asks a human", p.decide("write_file") is None)
    p.remember("write_file", "allow", "session")
    check("remember session applies", p.decide("write_file") == "allow")
    p.clear_session()
    check("clear_session forgets it", p.decide("write_file") is None)
    p.remember("write_file", "allow", "always")
    check("remember always survives a session clear", p.decide("write_file") == "allow")


def test_protocol():
    def handler(req):
        if req.get("op") == "echo":
            return {"echo": req.get("value")}
        return {"decision": "allow", "reason": "test"}

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "approvals.sock")
        server = SocketServer(path, handler)
        server.start()
        try:
            check("socket_request round-trips", socket_request(path, {"op": "echo", "value": 7})["echo"] == 7)
            check("approve decision comes back", socket_request(path, {"op": "approve"})["decision"] == "allow")

            bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bad.connect(path)
            bad.sendall(b'{"v": 99, "op": "echo"}\n')
            res = json.loads(bad.recv(65536).decode())
            check("protocol version mismatch fails closed", "error" in res and "version" in res["error"], str(res))
            bad.close()

            big = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            big.connect(path)
            big.sendall(b'{"v": 1, "pad": "' + b"x" * (MAX_FRAME_BYTES + 10) + b'"}\n')
            res = json.loads(big.recv(65536).decode())
            check("oversize frame is rejected", "error" in res and "exceeds" in res["error"], str(res)[:120])
            big.close()
        finally:
            server.stop()

        a, b = socket.socketpair()
        try:
            started = time.time()
            try:
                read_frame(a, timeout=0.3)
                check("idle socket raises ProtocolError", False)
            except ProtocolError as exc:
                check("idle socket raises ProtocolError", "idle" in str(exc) and time.time() - started < 2,
                      str(exc))
        finally:
            a.close()
            b.close()
    check("default idle timeout matches the original", IDLE_TIMEOUT_S == 30.0)


def test_surface_and_host():
    reg = ApprovalRegistry(sweep_interval_s=60)
    surface = SseSurface("tui:test", registry=reg, timeout_ms=2000)
    host = SurfaceHost()
    host.register(surface)
    check("host starts the surface", host.start()["ok"] == ["tui:test"])
    check("is_connected means a client is attached, not merely started", not surface.is_connected())
    check("host with no surface denies", host.request_approval("nope:hidden", "e", ApprovalUI("a", "t", "s", "/"))

          ["decision"] == "deny")

    queue = []
    surface.attach("local", queue)
    result = {}

    def ask():
        result.update(surface.request_approval("local", ApprovalUI("call-1", "write_file", "write out.txt",
                                                                  "/tmp", "tab-1", {"path": "out.txt"})))

    t = threading.Thread(target=ask)
    t.start()
    for _ in range(50):
        if queue:
            break
        time.sleep(0.02)
    check("the approval is rendered to the surface",
          queue and queue[0]["type"] == "approval_request" and queue[0]["tool_name"] == "write_file",
          str(queue[:1]))
    check("it is pending in the registry", reg.count() == 1)
    reg.resolve(queue[0]["approval_id"], "allow", "fine", remember="session")
    t.join(timeout=3)
    check("the parked caller gets the decision", result.get("decision") == "allow"
          and result.get("remember") == "session", str(result))

    surface.timeout_ms = 150
    result.clear()
    t2 = threading.Thread(target=ask)
    t2.start()
    t2.join(timeout=3)
    check("an unanswered approval denies on timeout", result.get("decision") == "deny"
          and "timed out" in (result.get("reason") or ""), str(result))
    host.stop()
    reg.stop()


def test_tab_loop():
    seen = []

    def executor(prompt, emit, cancel):
        seen.append(prompt.text)
        for i in range(5):
            if cancel.is_set():
                emit({"type": "aborted"})
                return
            emit({"type": "delta", "text": str(i)})
            time.sleep(0.02)
        emit({"type": "answer", "text": prompt.text.upper()})

    loop = TabLoop("tab-1", "TAB-1", executor)
    loop.start()
    first = loop.enqueue_prompt("one", {"state": ""})
    check("enqueue returns an id", bool(first))
    check("unknown id is not in the queue", loop.queue_length() <= 1)
    second = loop.enqueue_prompt("two")
    check("a second prompt queues behind the running turn", loop.queue_length() == 1 or loop.is_busy(),
          f"queued={loop.queue_length()} busy={loop.is_busy()}")
    time.sleep(0.5)
    check("both prompts ran in order", seen[:2] == ["one", "two"], str(seen))

    handle_before = loop._cancel
    loop.abort_turn()
    check("abort renews the handle before forwarding",
          loop._cancel is not handle_before and handle_before.is_set())

    slow = TabLoop("tab-2", "TAB-2", executor)
    slow.start()
    for i in range(MAX_QUEUED_PROMPTS + 5):
        slow.enqueue_prompt(f"p{i}")
    check("queue never exceeds the cap", slow.queue_length() <= MAX_QUEUED_PROMPTS, str(slow.queue_length()))
    slow.close()
    check("close marks the loop closed and releases waiters",
          slow.is_closed() and slow.waiter_count() == 0)

    reg = TabRegistry(executor, max_tabs=2)
    reg.get_or_create("a")
    reg.get_or_create("b")
    reg.get_or_create("c")
    check("max_tabs evicts the oldest", len(reg.tabs) == 2 and "a" not in reg.tabs, str(list(reg.tabs)))
    check("status reports each tab", {s["tab_id"] for s in reg.status()} == {"b", "c"})
    reg.close_all()
    loop.close()


def main():
    for name, fn in (("approval registry", test_registry), ("approval policy", test_policy),
                     ("socket protocol", test_protocol), ("surface + host", test_surface_and_host),
                     ("tab loop", test_tab_loop)):
        print(f"{name}:")
        fn()
    print()
    if failures:
        print(f"FAILED: {len(failures)} - {', '.join(failures)}")
        return 1
    print("hearth port ok: registry, policy, protocol, surfaces, tab loops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
