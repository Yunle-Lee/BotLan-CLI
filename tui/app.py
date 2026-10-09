#!/usr/bin/env python3
"""The jevstep agent conversation window.

Structure follows MiniMax Code's TUI source (packages/tui/src/tui/shell/*), reimplemented for this
stack: layout-policy.ts -> tui/layout.py, activity-line.ts -> tui/activity.py, status-line-items.ts +
workspace-status-line.ts -> tui/statusline.py, and the screen order below mirrors their shell
(welcome, transcript, panels, activity row, status line, composer, shortcut row).

None of their product is kept: no MiniMax account or provider plumbing, no build-mode/session-title/
git-branch/quota/subagent segments, no file at the same path, no module of theirs imported. The
backend is the Spark Duo orchestrator (Jev gate + GELab + the agent loop), our own endpoints.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Collapsible, Input, Markdown, Static

from tui import layout as layout_policy
from tui.activity import Activity
from tui.client import Event, OrchestratorUnreachable, SparkDuoClient
from tui.statusline import render as render_status

MODES = ("agent", "direct", "pipeline", "jev")
ENDPOINT = os.environ.get("JEVSTEP_ENDPOINT", "http://127.0.0.1:8090")
DEFAULT_SYSTEM = os.environ.get("JEVSTEP_SYSTEM", "You are a helpful assistant. Be accurate and concise.")

HELP = [
    "/mode <agent|direct|pipeline|jev>   what the keyboard is talking to",
    "   agent      Jev routes, the model works with tools, writes wait for your yes",
    "   direct     a plain conversation with the model",
    "   pipeline   the model answers from the context you hand it",
    "   jev        a calibrated verdict from the 0.8B decision model",
    "/ctx <file>        pipeline: the state to answer from (direct: a system note)",
    "/yes · /no [once|session|always]   answer the tool call that is waiting",
    "/approvals        what is waiting for an answer",
    "/rail             the steps and timings of the last turn (Esc closes)",
    "/history          the transcript of this conversation",
    "/trace            show or hide the timings row   ·   /clear  start over",
    "/quit             leave (Ctrl+C stops the running turn)",
]

CSS = """
Screen { layout: vertical; }
#welcome { height: auto; padding: 0 1; }
#body { height: 1fr; }
#transcript { width: 1fr; padding: 0 1; }
#overlay { height: 1fr; padding: 0 1; display: none; }
#overlay.open { display: block; }
#activity { height: 1; padding: 0 1; color: yellow; }
#timings { height: auto; padding: 0 1; color: gray; }
#status { height: 1; padding: 0 1; color: gray; }
#shortcuts { height: 1; padding: 0 1; color: gray; }
#prompt { dock: bottom; }
Collapsible { padding: 0; }
"""


class Window(App):
    CSS = CSS
    AUTO_FOCUS = "#prompt"
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "stop"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+e", "toggle_blocks", "details", show=False),
        Binding("ctrl+l", "clear_screen", "clear", show=False),
        Binding("escape", "close_panel", "close", show=False),
    ]

    def __init__(self, client=None, mode="agent", context="", image=None, force=False,
                 options=None, question=None) -> None:
        super().__init__()
        self.client = client or SparkDuoClient(ENDPOINT)
        self.mode = mode if mode in MODES else "agent"
        self.default_mode = "agent"
        self.context = context
        self.image = image
        self.force = force
        self.options = options or ["yes", "no"]
        self.system = DEFAULT_SYSTEM
        self.messages = [{"role": "system", "content": self.system}]
        self.trace = False
        self.detail_open = False
        self.panel_lines: list[str] = []
        self.rows: list[dict] = []
        self.blocks: list = []
        self.inflight: dict = {}
        self.pending: dict | None = None
        self.approved: set[str] = set()
        self.cancel = [False]
        self.initial_question = question
        self.activity = Activity()
        self.layout_policy = layout_policy.resolve(100, 30)
        self.sb: dict = {}
        self.model = os.environ.get("JEVSTEP_MODEL", "gelab-zero-4b")
        self.gate_context = int(os.environ.get("JEVSTEP_GATE_CONTEXT", "25600"))
        self.used_tokens = 0
        self.log_lines: list[str] = []
        self.rail_cache = ""
        self.welcome_text = ""
        self.status_value = ""
        self.timings = ""
        self._buf = ""
        self._stream = None

    # -- layout ------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="welcome")
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
        yield Static("", id="overlay")
        yield Static("", id="activity")
        yield Static("", id="timings")
        yield Static("", id="status")
        yield Static("", id="shortcuts")
        yield Input(placeholder="›  ask, or /help", id="prompt")

    def on_mount(self) -> None:
        self._resize()
        self.query_one("#prompt", Input).focus()
        self._welcome()
        self.probe_health()
        self.set_interval(0.25, self.refresh_activity)
        if self.initial_question:
            self.submit(self.initial_question)

    def on_resize(self, event) -> None:
        self._resize(event.size.width, event.size.height)

    def _resize(self, width: int | None = None, height: int | None = None) -> None:
        size = self.size
        self.layout_policy = layout_policy.resolve(width or size.width, height or size.height)
        pad = " " * self.layout_policy.padding
        self.query_one("#welcome", Static).styles.padding = (0, self.layout_policy.padding)
        self.query_one("#transcript", VerticalScroll).styles.padding = (0, self.layout_policy.padding)
        self.update_statusbar()
        self.update_shortcuts()

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    # -- the chrome ----------------------------------------------------------------------------
    def _welcome(self) -> None:
        width = self.layout_policy.columns - 2 * self.layout_policy.padding
        lines = [layout_policy.truncate("jevstep · Spark Duo", width),
                 layout_policy.truncate("Jev routes, the model answers, tools run when they are needed.",
                                        width)]
        if self.layout_policy.welcome != "single":
            lines.append(layout_policy.truncate(f"{ENDPOINT} · model {self.model}", width))
        self.welcome_text = "\n".join(lines)
        self.query_one("#welcome", Static).update(self.welcome_text)
        if self.layout_policy.welcome == "single":
            self.note("ready · /help", "gray")

    def update_shortcuts(self) -> None:
        hint = ("Ctrl+C stop · Ctrl+E details · Esc close · /help"
                if self.layout_policy.narrow else
                "Enter send · Ctrl+C stop · Ctrl+E steps · Ctrl+Q quit · Esc close panel · "
                "/ commands · /help")
        self.query_one("#shortcuts", Static).update(
            layout_policy.truncate(hint, self.layout_policy.columns - 2))

    def update_statusbar(self) -> None:
        gate_in_loop = self.mode in ("pipeline", "agent")
        parts = __import__("tui.statusline", fromlist=["segments"]).segments(
            root=self.sb.get("root"), allow_write=bool(self.sb.get("allow_write")),
            auto_approve=bool(self.sb.get("auto_approve")), model=self.model,
            gate_in_loop=gate_in_loop, context_limit=self.gate_context, used=self.used_tokens,
            mode=self.mode, default_mode=self.default_mode)
        # Kept in app state, not read back off the widget: what the app owns is testable and does not
        # depend on widget internals.
        self.status_value = render_status(parts, self.layout_policy)
        self.query_one("#status", Static).update(self.status_value)

    def refresh_activity(self) -> None:
        self.query_one("#activity", Static).update(
            self.activity.line(narrow=self.layout_policy.narrow))
        self.query_one("#timings", Static).update(self.timings if self.trace else "")

    def note(self, text: str, kind: str = "gray") -> None:
        self.log_lines.append(text)
        self.transcript.mount(Static(text, classes=f"note {kind}", markup=False))
        self.transcript.scroll_end(animate=False)

    def user_turn(self, text: str) -> None:
        self.log_lines.append("› " + text)
        self.transcript.mount(Static("› " + text, classes="user"))
        self.transcript.scroll_end(animate=False)

    def tool_block(self, header: str, body: str) -> None:
        """A finished tool call folds to one row; Ctrl+E opens the recent ones again."""
        block = Collapsible(Static(body, markup=False), title=header, collapsed=True)
        block.add_class("tool")
        self.blocks.append(block)
        self.log_lines.append(header)
        self.transcript.mount(block)
        self.transcript.scroll_end(animate=False)

    def render_rows(self) -> None:
        overlay = self.query_one("#overlay", Static)
        if not self.detail_open or not self.rows:
            overlay.remove_class("open")
            overlay.update("")
            self.rail_cache = ""
            return
        head = getattr(self, "_panel_head", ["steps · timings", ""])
        lines = []
        for r in self.rows:
            ms = f"{r.get('ms'):.0f}ms" if r.get("ms") is not None else ""
            lines.append(f"{r.get('glyph', '·')} {r.get('label', ''):<14} {ms:>7}  {r.get('detail', '')}"
                         .rstrip())
        body = "\n".join(head + lines)
        overlay.add_class("open")
        overlay.update(body + "\n\nEsc closes this panel.")
        self.rail_cache = body

    def open_panel(self, title: str, lines: list) -> None:
        self.detail_open = True
        self._panel_head = [title, ""]
        overlay = self.query_one("#overlay", Static)
        overlay.add_class("open")
        overlay.update("\n".join([title, ""] + lines + ["", "Esc closes this panel."]))
        self.rail_cache = "\n".join(lines)

    def close_panel(self) -> None:
        self.detail_open = False
        self._panel_head = []
        self.render_rows()

    # -- health --------------------------------------------------------------------------------
    @work(thread=True, exclusive=False)
    def probe_health(self) -> None:
        try:
            info = self.client.health()
        except OrchestratorUnreachable as exc:
            self.call_from_thread(self.note, str(exc), "error")
            return
        agent = info.get("agent") or {}
        policy = (info.get("approvals") or {}).get("policy") or {}

        def absorb() -> None:
            self.sb = {"root": agent.get("root"), "allow_write": agent.get("allow_write"),
                       "auto_approve": policy.get("auto_approve")}
            self.update_statusbar()

        self.call_from_thread(absorb)

    # -- the turn ------------------------------------------------------------------------------
    def submit(self, question: str) -> None:
        self.close_panel()
        self.user_turn(question)
        self._buf, self._stream = "", None
        self.rows = []
        self.timings = ""
        self.activity.start("running", "thinking")
        self.refresh_activity()
        self.run_turn(question)

    @work(thread=True, exclusive=True, group="turn")
    def run_turn(self, question: str) -> None:
        self.cancel = [False]
        try:
            if self.mode == "agent":
                events = self.client.agent_stream(question, self.context, self.image, self.cancel,
                                                  approve=sorted(self.approved))
                self.image = None
            elif self.mode == "pipeline":
                events = self.client.answer_stream(self.context, question, self.image, self.force,
                                                   self.cancel)
                self.image = None
            elif self.mode == "direct":
                if self.image:
                    self.messages.append({"role": "user", "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": self.image}}]})
                    self.image = None
                else:
                    self.messages.append({"role": "user", "content": question})
                events = self.client.chat_stream(self.messages, cancel=self.cancel)
            else:
                res = self.client.decide(self.context, question, self.options)
                self.call_from_thread(self.show_verdict, res)
                return
            for ev in events:
                self.call_from_thread(self.handle_event, ev, question)
        except (OrchestratorUnreachable, RuntimeError) as exc:
            self.call_from_thread(self.note, str(exc), "error")
            self.call_from_thread(self.finish_quiet)

    def finish_quiet(self) -> None:
        self.activity.settle("error")
        self.refresh_activity()

    def handle_event(self, ev: Event, question: str) -> None:
        if ev.name == "gate":
            self.rows.append({"glyph": "·", "label": "gate",
                              "detail": f"{ev.data.get('intent')} -> {ev.data.get('branch')}",
                              "ms": ev.data.get("gate_ms")})
        elif ev.name == "step":
            self.activity.start("running", f"step {ev.data.get('n')}")
            self.refresh_activity()
        elif ev.name == "tool_call":
            args = json.dumps(ev.data.get("arguments") or {}, ensure_ascii=False)
            self.inflight[ev.data.get("id")] = {"name": str(ev.data.get("name")), "args": args}
            self.rows.append({"glyph": "·", "label": str(ev.data.get("name")), "detail": args[:70]})
            self.activity.start("running", f"{ev.data.get('name')} {args[:40]}")
            self.refresh_activity()
        elif ev.name == "tool_result":
            call = self.inflight.pop(ev.data.get("id"), {})
            ms, size = ev.data.get("ms"), ev.data.get("bytes") or 0
            outcome = f"{size}b" if ev.data.get("ok") else str(ev.data.get("error"))[:60]
            for row in reversed(self.rows):
                if row.get("label") == call.get("name") and row.get("ms") is None:
                    row.update({"ms": ms, "detail": f"{row.get('detail','')}  ->  {outcome}"})
                    break
            self.tool_block(f"{call.get('name', 'tool')} {call.get('args', '')} → {outcome}"
                            f"   {f'{ms:.0f}ms' if ms else ''}".strip(),
                            call.get("args", "") + "\n" + (ev.data.get("preview") or ""))
        elif ev.name == "approval":
            self.pending = {"id": ev.data.get("id"), "tool": ev.data.get("tool")}
            self.activity.start("running", f"{ev.data.get('tool')} is waiting for your yes/no")
            self.refresh_activity()
            self.note(f"approval needed: {ev.data.get('tool')} "
                      f"{json.dumps(ev.data.get('arguments') or {}, ensure_ascii=False)[:80]}", "error")
            self.note("  answer with /yes or /no", "gray")
        elif ev.name == "verify":
            self.rows.append({"glyph": "·", "label": "verify",
                              "detail": str(ev.data.get("relation")), "ms": ev.data.get("ms")})
        elif ev.name == "delta":
            self.stream_piece(ev.data.get("text", ""))
        elif ev.name == "done":
            self.finish_turn(ev.data)
        elif ev.name in ("error", "message"):
            self.note(str(ev.data), "error")

    def stream_piece(self, text: str) -> None:
        if not text:
            return
        if self._stream is None:
            self._stream = Static("", markup=False)
            self.transcript.mount(self._stream)
        self._buf += text
        self.activity.tokens_per_second = None
        self._stream.update(self._buf)
        self.transcript.scroll_end(animate=False)

    def finish_turn(self, out: dict) -> None:
        reply = (out.get("answer") or out.get("reply") or self._buf or "").strip()
        if self._stream is not None:
            self._stream.remove()
            self._stream = None
        if reply:
            self.log_lines.append(reply)
            self._buf = ""
            self.transcript.mount(Markdown(reply))
        if out.get("escalate"):
            self.note("escalated: " + str(out.get("reason") or "not answered"), "error")
        if out.get("verify"):
            self.rows.append({"glyph": "·", "label": "verify",
                              "detail": str(out["verify"].get("relation")),
                              "ms": (out.get("stages") or {}).get("verify_ms")})
        if out.get("gate_tokens"):
            self.used_tokens = int(out["gate_tokens"])
        stages = out.get("stages") or {}
        self.timings = " · ".join(f"{k[:-3]}={v:.0f}ms" for k, v in stages.items()
                                  if k.endswith("_ms"))
        if out.get("stop_reason"):
            self.timings += f" · stop={out['stop_reason']} tools={out.get('tool_calls', 0)}"
        self.activity.settle("idle")
        self.refresh_activity()
        self.update_statusbar()
        self.transcript.scroll_end(animate=False)

    def show_verdict(self, res: dict) -> None:
        probs = " ".join(f"{k}={v:.3f}" for k, v in
                         sorted((res.get("probabilities") or {}).items(), key=lambda x: -x[1]))
        self.transcript.mount(Static(f"verdict: {res.get('verdict')}  p={res.get('confidence', 0):.3f}",
                                     markup=False))
        self.note(probs, "gray")
        self.rows = [{"glyph": "·", "label": "jev", "detail": str(res.get("verdict")),
                      "ms": res.get("ms")}]
        self.activity.settle("idle")
        self.refresh_activity()

    # -- input --------------------------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self.command(text)
        else:
            self.submit(text)

    def command(self, line: str) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
        elif cmd in ("/help", "/?", "/h"):
            self.open_panel("help · commands", ["  " + h for h in HELP])
        elif cmd == "/mode":
            if arg in MODES:
                self.mode = arg
                self.update_statusbar()
                self.note(f"mode = {arg}", "gray")
            else:
                self.note("modes: " + ", ".join(MODES), "gray")
        elif cmd == "/ctx":
            if not arg:
                self.note(f"context {len(self.context)} chars", "gray")
            else:
                try:
                    self.context = Path(arg).expanduser().read_text()
                except OSError as exc:
                    self.note(f"cannot read {arg}: {exc}", "error")
                    return
                self.note(f"context loaded: {len(self.context)} chars from {arg}", "gray")
        elif cmd in ("/yes", "/no"):
            if not self.pending:
                self.note("nothing is waiting for an answer", "gray")
                return
            remember = next((w for w in ("once", "session", "always") if w in arg.split()), None)
            try:
                res = self.client.approve(self.pending["id"],
                                          "allow" if cmd == "/yes" else "deny", remember)
            except Exception as exc:
                self.note(f"cannot answer the approval: {exc}", "error")
                return
            self.note(f"{self.pending['tool']}: {res.get('decision')} "
                      f"{'resolved' if res.get('resolved') else 'already gone'}", "gray")
            self.pending = None
        elif cmd == "/pre":
            self.approved = set() if arg in ("", "off") else {arg}
            self.note(f"pre-approved: {sorted(self.approved) or 'none'}", "gray")
        elif cmd == "/approvals":
            try:
                pend = self.client.approvals()
            except Exception as exc:
                self.note(f"cannot read approvals: {exc}", "error")
                return
            self.open_panel("approvals", [f"  {p['id'][:8]}  {p['tool_name']:12} {p['summary']}"
                                          for p in pend] or ["  nothing pending"])
        elif cmd == "/rail":
            self.detail_open = not self.detail_open
            self._panel_head = ["steps · timings", ""]
            self.render_rows()
            self.note(f"steps panel = {'open' if self.detail_open else 'closed'}", "gray")
        elif cmd == "/history":
            rows = [(m["role"] + ": " + (m["content"] if isinstance(m["content"], str)
                                         else "(image + text)")[:120]) for m in self.messages[1:]]
            self.open_panel("history", rows or ["(nothing yet)"])
        elif cmd == "/trace":
            self.trace = not self.trace
            self.refresh_activity()
            self.note(f"trace = {self.trace}", "gray")
        elif cmd == "/clear":
            self.action_clear_screen()
            self.note("cleared", "gray")
        elif cmd == "/force":
            self.force = not self.force
            self.note(f"force = {self.force}", "gray")
        else:
            self.note("unknown command · /help", "error")

    # -- actions ------------------------------------------------------------------------------
    def action_interrupt(self) -> None:
        self.cancel[0] = True
        self.activity.settle("stopping")
        self.refresh_activity()
        self.note("stopped", "gray")

    def action_close_panel(self) -> None:
        self.close_panel()

    def action_toggle_blocks(self) -> None:
        recent = self.blocks[-12:]
        if not recent:
            self.note("no tool calls in this conversation yet", "gray")
            return
        opening = recent[-1].collapsed
        for block in recent:
            block.collapsed = not opening
        self.note(f"{'opened' if opening else 'folded'} {len(recent)} tool block(s)", "gray")

    def action_clear_screen(self) -> None:
        self.transcript.remove_children()
        self.rows, self.blocks, self.log_lines = [], [], []
        self._buf = ""
        self.render_rows()

    # -- testability ---------------------------------------------------------------------------
    def transcript_text(self) -> str:
        return "\n".join(self.log_lines + ([self._buf] if self._buf else []))

    def panel_text(self) -> str:
        return self.rail_cache

    def status_text(self) -> str:
        return self.status_value

    def activity_text(self) -> str:
        return self.activity.line(narrow=self.layout_policy.narrow)


def main() -> None:
    ap = argparse.ArgumentParser(description="the Spark Duo agent window")
    ap.add_argument("question", nargs="*", help="one-shot question (omit for interactive)")
    ap.add_argument("-m", "--mode", choices=MODES, default=os.environ.get("JEVSTEP_MODE"))
    ap.add_argument("-c", "--context", help="context file (pipeline: the state)")
    ap.add_argument("-i", "--image", help="image file to attach")
    ap.add_argument("-o", "--options", help="jev mode: comma-separated options")
    ap.add_argument("-f", "--force", action="store_true", help="answer past a low-confidence gate")
    a = ap.parse_args()

    context = Path(a.context).expanduser().read_text() if a.context else ""
    mode = a.mode or "agent"
    image = None
    if a.image:
        path = Path(a.image).expanduser()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        image = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    Window(mode=mode, context=context, image=image, force=a.force,
           options=[o.strip() for o in a.options.split(",")] if a.options else None,
           question=" ".join(a.question) if a.question else None).run()


if __name__ == "__main__":
    main()
