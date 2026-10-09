#!/usr/bin/env python3
"""One row that reports what the run is doing, adapted from MiniMax Code's
packages/tui/src/tui/shell/activity-line.ts.

Their rules, kept because they are the reason the row works:
  * one row, and it absorbs the composer's header row so the same moment is never explained twice;
  * elapsed time is noise before the user's patience starts draining - hidden until 1.5 s;
  * the stop control is the last thing to survive a narrow terminal.

Their phase set is trimmed to the ones this stack actually reaches - loading, running, stopping,
error - and their animation interval is kept (80 ms) with our own frames.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

FRAMES = "|/-\\"                    # ours, not the Braille set theirs uses
FRAME_MS = 80
ELAPSED_REVEAL_AFTER_S = 1.5

PHASES = ("idle", "loading", "running", "stopping", "error")


@dataclass
class Activity:
    phase: str = "idle"
    message: str = ""
    started_at: Optional[float] = None
    tokens_per_second: Optional[float] = None
    error_settled: bool = False
    controls: list = field(default_factory=list)      # steer / details / stop

    def start(self, phase: str = "running", message: str = "") -> None:
        self.phase = phase
        self.message = message
        self.error_settled = False
        self.started_at = time.time()

    def settle(self, phase: str = "idle") -> None:
        self.phase = phase
        self.started_at = None

    @property
    def busy(self) -> bool:
        return self.phase in ("loading", "running", "stopping")

    def frame(self, now: Optional[float] = None) -> str:
        if not self.busy:
            return ""
        t = now if now is not None else time.time()
        return FRAMES[int(t * 1000 / FRAME_MS) % len(FRAMES)]

    def line(self, narrow: bool = False, now: Optional[float] = None) -> str:
        if self.phase == "idle":
            return ""
        t = now if now is not None else time.time()
        bits = [f"{self.frame(t)} {self.phase}"]
        if self.message:
            bits.append(self.message)
        if self.started_at is not None and t - self.started_at >= ELAPSED_REVEAL_AFTER_S:
            bits.append(f"{t - self.started_at:.0f}s")
        if self.tokens_per_second:
            bits.append(f"{self.tokens_per_second:.1f} tok/s")
        if not narrow:
            controls = self.controls or ["stop"]
            hints = {"stop": "Ctrl+C stop", "steer": "type to steer", "details": "Ctrl+E details"}
            bits += [hints[c] for c in controls if c in hints]
        else:
            bits.append("Ctrl+C stop")               # the last control to survive a narrow terminal
        return " · ".join(bits)
