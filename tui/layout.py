#!/usr/bin/env python3
"""Responsive density, adapted from MiniMax Code's packages/tui/src/tui/shell/layout-policy.ts.

Their policy: three densities chosen from the terminal size, and everything else (padding, how much
transcript to keep beyond the viewport, how the welcome block lays out) derives from the density
instead of being decided per component. Same idea here, same thresholds, our own welcome layouts:

    columns < 48  or rows < 12   -> compact    padding 0, overscan 4
    columns >= 72                -> wide       padding 2, overscan 12
    otherwise                    -> standard   padding 1, overscan 8
"""
from __future__ import annotations

from dataclasses import dataclass

COMPACT_COLUMNS = 48
WIDE_COLUMNS = 72
COMPACT_ROWS = 12


@dataclass(frozen=True)
class Layout:
    columns: int
    rows: int
    density: str            # compact | standard | wide
    welcome: str            # single | stacked | split
    padding: int            # horizontal padding
    overscan: int           # transcript rows kept beyond the viewport

    @property
    def narrow(self) -> bool:
        return self.density == "compact"


def resolve(columns: int, rows: int = 24) -> Layout:
    cols = max(1, int(columns or 1))
    rws = max(1, int(rows or 1))
    compact = cols < COMPACT_COLUMNS or rws < COMPACT_ROWS
    wide = not compact and cols >= WIDE_COLUMNS
    density = "compact" if compact else "wide" if wide else "standard"
    return Layout(columns=cols, rows=rws, density=density,
                  welcome="single" if compact else "split" if wide else "stacked",
                  padding=0 if compact else 2 if wide else 1,
                  overscan=4 if compact else 12 if wide else 8)


def truncate(text: str, width: int) -> str:
    """Width-aware truncation: CJK glyphs are two cells wide, so a naive slice overflows."""
    if width <= 0:
        return ""
    total = 0
    out = []
    for ch in text:
        w = 2 if _wide(ch) else 1
        if total + w > width:
            break
        out.append(ch)
        total += w
    return "".join(out)


def display_width(text: str) -> int:
    return sum(2 if _wide(ch) else 1 for ch in text)


def pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _wide(ch: str) -> bool:
    code = ord(ch)
    return (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6 or 0x1F300 <= code <= 0x1FAFF)
