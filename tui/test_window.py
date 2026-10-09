#!/usr/bin/env python3
"""Headless checks for the window: the chrome renders, the panel opens and closes, a tool call folds.

Run: spark-duo/.venv/bin/python spark-duo/tui/test_window.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Input

from tui import layout as layout_policy
from tui.activity import Activity
from tui.app import Window
from tui.client import Event

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


AGENT_EVENTS = [
    Event("gate", {"intent": "record_lookup", "branch": "needs_lookup", "confidence": 0.94,
                   "gate_ms": 51.0}),
    Event("step", {"n": 1, "phase": "think"}),
    Event("tool_call", {"n": 1, "id": "call_0", "name": "read_file",
                        "arguments": {"path": "config.json"}}),
    Event("tool_result", {"n": 1, "id": "call_0", "tool": "read_file", "ok": True, "ms": 3.1,
                          "bytes": 4100, "error": None, "preview": "config.json (140 lines)"}),
    Event("delta", {"text": "The agent root is "}),
    Event("delta", {"text": "~/spark-duo."}),
    Event("verify", {"relation": "supported", "probability": 0.81, "ms": 24.0}),
    Event("done", {"answer": "The agent root is ~/spark-duo.", "route": "needs_lookup",
                   "intent": "record_lookup", "escalate": False, "tool_calls": 1,
                   "stop_reason": "answer", "gate_tokens": 811,
                   "stages": {"gate_ms": 51.0, "tools_ms": 3.1, "verify_ms": 24.0, "total_ms": 320.0}}),
]


class FakeClient:
    def health(self):
        return {"vlm": "http://127.0.0.1:8080", "agent": {"root": "/home/user1/spark-duo",
                                                          "allow_write": True},
                "approvals": {"policy": {"auto_approve": [], "auto_deny": ["shell"]}},
                "sampling": {"repeat_penalty": 1.1}}

    def agent_stream(self, *a, **k):
        return iter(AGENT_EVENTS)

    def answer_stream(self, *a, **k):
        return iter(AGENT_EVENTS)

    def chat_stream(self, *a, **k):
        return iter(AGENT_EVENTS)

    def approvals(self):
        return [{"id": "abc12345", "tool_name": "write_file", "summary": "write 4 bytes to x.txt"}]


def test_layout():
    compact = layout_policy.resolve(40, 10)
    standard = layout_policy.resolve(60, 24)
    wide = layout_policy.resolve(100, 40)
    check("compact under 48 columns or 12 rows", compact.density == "compact" and compact.padding == 0,
          compact.density)
    check("standard in between", standard.density == "standard" and standard.padding == 1)
    check("wide from 72 columns", wide.density == "wide" and wide.padding == 2
          and wide.overscan == 12)
    check("welcome layout follows density", (compact.welcome, standard.welcome, wide.welcome)
          == ("single", "stacked", "split"))
    check("CJK takes two cells", layout_policy.display_width("中文ab") == 6)
    check("truncate respects width", layout_policy.truncate("中文中文", 5) == "中文")


def test_activity():
    a = Activity()
    check("idle renders nothing", a.line() == "")
    a.start("running", "thinking")
    line = a.line(now=a.started_at + 0.2)
    check("running shows a frame and the message", line.startswith(("|", "/", "-", "\\"))
          and "thinking" in line, line)
    check("elapsed is hidden before 1.5 s", "0s" not in line, line)
    later = a.line(now=a.started_at + 3.0)
    check("elapsed appears after 1.5 s", "3s" in later, later)
    check("narrow keeps only stop", "Ctrl+C stop" in a.line(narrow=True, now=a.started_at + 1))
    a.settle("idle")
    check("settled is idle", a.line() == "")


async def smoke() -> None:
    app = Window(client=FakeClient(), mode="agent")
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        check("welcome names the window", "jevstep" in app.welcome_text, app.welcome_text[:60])
        check("status line shows the model and the gate",
              "gelab-zero-4b" in app.status_text() and "Jev gate" in app.status_text(),
              app.status_text())
        check("status line shows the approval mode", "Ask" in app.status_text(), app.status_text())
        check("nothing is running yet", app.activity_text() == "")

        app.query_one(Input).value = "where is the agent root?"
        await pilot.press("enter")
        await pilot.pause(0.5)
        check("the question is in the transcript", "where is the agent root?" in app.transcript_text())
        check("the answer is in the transcript", "~/spark-duo" in app.transcript_text())
        check("the tool call folded to one row", "read_file" in app.transcript_text()
              and app.blocks and app.blocks[0].collapsed)
        check("the activity line settles", app.activity_text() == "")
        check("the used-token count reached the status line", "used 811" in app.status_text(),
              app.status_text())

        app.command("/rail")
        check("panel opens with the steps", "gate" in app.panel_text()
              and "read_file" in app.panel_text(), app.panel_text())
        app.action_close_panel()
        check("Esc closes it", app.panel_text() == "")

        app.command("/approvals")
        check("approvals panel lists the pending call", "write_file" in app.panel_text())
        app.action_close_panel()

        app.command("/mode")
        check("bad mode is refused", app.mode == "agent")
        app.command("/mode direct")
        check("mode switches", app.mode == "direct" and "Jev gate" not in app.status_text())
        app.command("/nope")
        check("unknown command says so", "unknown command" in app.transcript_text())

    small = Window(client=FakeClient(), mode="agent")
    async with small.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        check("compact terminal still renders a status line", bool(small.status_text()))


def main():
    print("layout policy:")
    test_layout()
    print("activity line:")
    test_activity()
    print("window:")
    asyncio.run(smoke())
    print()
    if failures:
        print(f"FAILED: {len(failures)} - {', '.join(failures)}")
        return 1
    print("window ok: layout, activity, chrome, transcript, panels, modes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
