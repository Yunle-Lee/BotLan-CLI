#!/usr/bin/env python3
"""The status line: a few segments, widest-first, adapted from MiniMax Code's
packages/tui/src/tui/shell/status-line-items.ts + workspace-status-line.ts.

What is kept from theirs: the idea of named segments with aliases, of assembling them widest-first
and dropping whole segments as the terminal narrows, and of never inventing a value.

What is deliberately NOT kept: their segment catalogue. They have sixteen (build mode, session title,
git branch, PR link, plan mode, subagents, quota, cache ratio, ...). This stack has four, and a
segment we cannot fill honestly does not appear:

    dir        the agent's workspace root
    approval   Read-only | Ask | Auto, from the live approval policy
    model      the model, plus whether the calibrated gate is in the loop
    context    the gate's input budget, and what the last turn actually used
"""
from __future__ import annotations

from typing import List, Optional

from tui.layout import Layout, truncate

SEPARATOR = " │ "


def segments(*, root: Optional[str], allow_write: bool, auto_approve: bool, model: str,
             gate_in_loop: bool, context_limit: int, used: int = 0, mode: str = "",
             default_mode: str = "agent") -> List[str]:
    where = (root or "~").replace(str(__import__("pathlib").Path.home()), "~")
    approval = "Read-only" if not allow_write else "Auto" if auto_approve else "Ask"
    model_seg = f"✦ {model}" + (" · Jev gate" if gate_in_loop else "")
    ctx = f"Context {context_limit}" + (f" · used {used}" if used else "")
    out = [where, approval, model_seg, ctx]
    if mode and mode != default_mode:
        out.append(f"mode {mode}")
    return out


def render(parts: List[str], layout: Layout) -> str:
    """Drop trailing segments until it fits: the leftmost are the ones that always survive."""
    keep = list(parts)
    while keep:
        line = SEPARATOR.join(keep)
        if len(line) <= layout.columns - 2 * layout.padding:
            return line
        if len(keep) == 1:
            return truncate(keep[0], max(4, layout.columns - 2 * layout.padding))
        keep.pop()
    return ""
