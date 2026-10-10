#!/usr/bin/env python3
"""The Textual face of botlan_setup.py: five steps, one screen each.

  1 Bot      name + color
  2 Scope    treemap of the top dirs under $HOME (area ~ size); arrows move, space selects,
             enter goes on. Nothing selected = the whole home.
  3 Model    jev-step (this Spark's stack, started in the background if missing) | local | api
  4 Budget   memory GB + CPU %, against what the gateway can still assign
  5 Done     the Bot is created; its pairing info and the laptop's next step

All the work is in botlan_setup.py; this file only draws it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Label, RadioButton, RadioSet, Static

import botlan_setup as core

CSS = """
Screen { padding: 1 2; }
.title { text-style: bold; color: $accent; margin-bottom: 1; }
.hint { color: $text-muted; }
Input { width: 48; margin-bottom: 1; }
RadioSet { margin-bottom: 1; }
#treemap { height: 1fr; min-height: 12; border: round $panel; }
#picked { height: 2; }
#log { height: auto; max-height: 8; color: $text-muted; }
.row { height: auto; }
Button { margin-right: 2; }
"""


class Draft:
    """What the steps collect."""
    name = "Vision"
    color = core.COLORS[0]
    scope: list[str] = []
    backend = "jev-step"
    base_url = model = api_key = ""
    mem_gb = 16.0
    cpu_pct = 400.0


class Treemap(Widget, can_focus=True):
    """Dirs as rectangles, area ~ size. One rect has the cursor; selected rects are filled."""

    BINDINGS = [Binding("left", "move(-1,0)", show=False), Binding("right", "move(1,0)", show=False),
                Binding("up", "move(0,-1)", show=False), Binding("down", "move(0,1)", show=False),
                Binding("space", "toggle", "select"), Binding("enter", "done", "next")]

    def __init__(self, dirs: list[dict], selected: list[str], **kw) -> None:
        super().__init__(**kw)
        self.dirs, self.selected, self.cursor, self.rects = dirs, set(selected), 0, []

    def render(self) -> Text:
        w, h = max(self.size.width, 10), max(self.size.height, 4)
        grid, self.rects = core.treemap_cells(self.dirs, w, h)
        palette = ["#2b3a1a", "#1d2f45", "#40261d", "#33263f", "#3f3a1f", "#3f1f2a"]
        out = Text()
        for row in grid:
            for ch, i in row:
                if i < 0:
                    out.append(ch)
                    continue
                path = self.rects[i][0]
                style = f"on {palette[i % len(palette)]}"
                if path in self.selected:
                    style = f"bold black on {Draft.color}"
                if i == self.cursor:
                    style += " reverse"
                out.append(ch, style)
            out.append("\n")
        return out

    def action_move(self, dx: int, dy: int) -> None:
        """Go to the nearest rectangle whose centre lies in that direction."""
        if not self.rects:
            return
        _, x, y, w, h = self.rects[self.cursor]
        cx, cy = x + w / 2, y + h / 2
        best, dist = self.cursor, None
        for i, (_, x2, y2, w2, h2) in enumerate(self.rects):
            ox, oy = x2 + w2 / 2 - cx, y2 + h2 / 2 - cy
            if (dx and ox * dx <= 0) or (dy and oy * dy <= 0):
                continue
            d = abs(ox) + abs(oy) * 2 + (abs(oy) * 3 if dx else abs(ox))   # prefer the same row/col
            if dist is None or d < dist:
                best, dist = i, d
        self.cursor = best
        self.refresh()
        self.post_message(self.Changed())

    def action_toggle(self) -> None:
        if not self.rects:
            return
        path = self.rects[self.cursor][0]
        if path == "…":
            return
        self.selected ^= {path}
        self.refresh()
        self.post_message(self.Changed())

    def action_done(self) -> None:
        self.app.screen.next()

    class Changed(Message):
        """The cursor moved or the selection changed."""


class Step(Screen):
    BINDINGS = [Binding("escape", "back", "back"), Binding("ctrl+c", "app.quit", "quit")]

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()


class BotStep(Step):
    def compose(self) -> ComposeResult:
        yield Label("1 / 5  Your first Bot", classes="title")
        yield Label("Name")
        yield Input(Draft.name, id="name")
        yield Label("Color")
        with RadioSet(id="color"):
            for c in core.COLORS:
                yield RadioButton(Text("██ ", style=c) + Text(c), value=c == Draft.color)
        yield Button("Next", id="next", variant="primary")
        yield Footer()

    def next(self) -> None:
        Draft.name = self.query_one("#name", Input).value.strip()[:40] or "Bot"
        Draft.color = core.COLORS[max(self.query_one("#color", RadioSet).pressed_index, 0)]
        self.app.push_screen(ScopeStep())

    def on_button_pressed(self, _) -> None:
        self.next()

    def on_input_submitted(self, _) -> None:
        self.next()


class ScopeStep(Step):
    def compose(self) -> ComposeResult:
        yield Label("2 / 5  Activity scope", classes="title")
        yield Static("Where this Bot's commands may read and write. Arrows move, space selects, "
                    "enter continues. Nothing selected = the whole home.", classes="hint")
        yield Static("scanning $HOME (4 s budget)…", id="picked")
        yield Vertical(id="slot")
        yield Footer()

    def on_mount(self) -> None:
        self.scan()

    @work(thread=True)
    def scan(self) -> None:
        data = core.scan_home()
        self.app.call_from_thread(self.show, data)

    def show(self, data: dict) -> None:
        self.data = data
        tm = Treemap(data["dirs"], Draft.scope, id="treemap")
        self.query_one("#slot").mount(tm)
        tm.focus()
        self.update_picked()

    def on_treemap_changed(self, _) -> None:
        self.update_picked()

    def update_picked(self) -> None:
        tm = self.query_one(Treemap)
        cur = tm.rects[tm.cursor][0] if tm.rects else ""
        size = next((d["size"] for d in self.data["dirs"] if d["path"] == cur), 0)
        picked = sorted(tm.selected) or [self.data["home"] + "  (whole home)"]
        self.query_one("#picked", Static).update(
            f"cursor: {cur} {core.human(size)}    scope: {', '.join(picked)}")

    def next(self) -> None:
        Draft.scope = sorted(self.query_one(Treemap).selected)
        self.app.push_screen(ModelStep())


class ModelStep(Step):
    def compose(self) -> ComposeResult:
        yield Label("3 / 5  Model backend", classes="title")
        with RadioSet(id="backend"):
            yield RadioButton("jev-step  Jev-0.8B router + GELab-Zero-4B on this Spark", value=True)
            yield RadioButton("local     another OpenAI-compatible server on this Spark")
            yield RadioButton("api       a hosted OpenAI-compatible API")
        yield Static("", id="log")
        yield Input(placeholder="base URL (…/v1)", id="base_url")
        yield Input(placeholder="model", id="model")
        yield Input(placeholder="API key", password=True, id="api_key")
        yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.on_radio_set_changed(None)

    def on_radio_set_changed(self, _) -> None:
        Draft.backend = core.BACKENDS[max(self.query_one("#backend", RadioSet).pressed_index, 0)]
        for wid in ("base_url", "model", "api_key"):
            self.query_one(f"#{wid}").display = Draft.backend != "jev-step"
        self.query_one("#api_key").display = Draft.backend == "api"
        self.detect()

    @work(thread=True, exclusive=True)
    def detect(self) -> None:
        say = lambda t: self.app.call_from_thread(self.query_one("#log", Static).update, t)  # noqa: E731
        if Draft.backend == "jev-step":
            state = core.stack_state()
            steps = core.stack_steps(state)
            if not steps:
                say("found: llama-server :8080, Jev orchestrator :8090, gateway - reusing them")
                return
            say(f"missing: {', '.join(steps)} - running in the background (logs/setup-stack.log)")
            proc = core.start_stack(steps)
            while proc.poll() is None:
                tail = (core.LOGS / "setup-stack.log").read_text(errors="replace").splitlines()[-3:]
                say("\n".join(tail))
                time.sleep(2)
            say("stack ready" if proc.returncode == 0 else f"stack step failed ({proc.returncode})")
        elif Draft.backend == "local":
            say("probing " + ", ".join(map(str, core.LOCAL_PORTS)) + " …")
            found = core.probe_local()
            if not found:
                say("no OpenAI-compatible /v1/models answered on those ports")
                return
            first = found[0]
            say("\n".join(f"{f['base_url']}  {', '.join(f['models'][:3])}" for f in found))
            self.app.call_from_thread(self._fill, first["base_url"], first["models"][0])
        else:
            say("any OpenAI-compatible API; the key is stored only in the zones file (0600)")

    def _fill(self, base: str, model: str) -> None:
        self.query_one("#base_url", Input).value = base
        self.query_one("#model", Input).value = model

    def on_button_pressed(self, _) -> None:
        self.next()

    def next(self) -> None:
        Draft.base_url = self.query_one("#base_url", Input).value.strip()
        Draft.model = self.query_one("#model", Input).value.strip()
        Draft.api_key = self.query_one("#api_key", Input).value.strip()
        if Draft.backend != "jev-step" and not (Draft.base_url and Draft.model):
            self.query_one("#log", Static).update("base URL and model are needed")
            return
        self.app.push_screen(BudgetStep())


class BudgetStep(Step):
    def compose(self) -> ComposeResult:
        yield Label("4 / 5  Resources", classes="title")
        yield Static("asking the gateway…", id="budget")
        yield Label("Memory GB (CPU-side, enforced by the slice; size GPU use to it yourself)")
        yield Input(str(int(Draft.mem_gb)), id="mem")
        yield Label("CPU %  (100 = one core)")
        yield Input(str(int(Draft.cpu_pct)), id="cpu")
        yield Button("Create Bot", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    @work(thread=True)
    def load(self) -> None:
        b = core.budget()
        self.app.call_from_thread(self.query_one("#budget", Static).update,
                                  f"{b['assignable_gb']} GB assignable of {b['mem_total_gb']} GB "
                                  f"({b['reserved_gb']} GB kept for Spark Duo and the OS)")

    def on_button_pressed(self, _) -> None:
        self.next()

    def next(self) -> None:
        try:
            Draft.mem_gb = float(self.query_one("#mem", Input).value)
            Draft.cpu_pct = float(self.query_one("#cpu", Input).value)
        except ValueError:
            self.query_one("#budget", Static).update("memory and CPU must be numbers")
            return
        self.app.push_screen(DoneStep())


class DoneStep(Step):
    def compose(self) -> ComposeResult:
        yield Label("5 / 5  Pairing", classes="title")
        yield Static("creating…", id="out")
        yield Button("Finish", id="finish", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.create()

    @work(thread=True)
    def create(self) -> None:
        res = core.create_bot(Draft.name, Draft.color, Draft.mem_gb, Draft.cpu_pct, Draft.scope,
                              Draft.backend, Draft.base_url, Draft.model, Draft.api_key)
        text = (f"not created: {res['error']}\n(esc to go back)" if "error" in res
                else core.pairing_text(res))
        self.app.call_from_thread(self.query_one("#out", Static).update, text)
        self.app.result = 0 if "error" not in res else 1

    def on_button_pressed(self, _) -> None:
        self.app.exit()

    def next(self) -> None:
        self.app.exit()


class SetupApp(App):
    TITLE = "BotLan setup"
    CSS = CSS
    result = 1

    def on_mount(self) -> None:
        self.push_screen(BotStep())


def run() -> int:
    app = SetupApp()
    app.run()
    return app.result


if __name__ == "__main__":
    sys.exit(run())
